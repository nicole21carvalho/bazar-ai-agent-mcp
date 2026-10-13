"""Tokens do Keycloak: validação (resource server) e troca de token (RFC 8693).

Pelas boas práticas de segurança do MCP, um servidor nunca repassa adiante o token
que recebeu ("token passthrough"): ele só aceita tokens emitidos para ele mesmo e,
para chamar outro serviço, troca o token por um novo, válido só para aquele serviço,
mas ainda em nome do mesmo cliente. Se o token de um serviço vazar, ele não abre
nenhum outro.
"""

import hashlib
import logging
import time
from typing import Any

import anyio
import httpx
import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import SecretStr

log = logging.getLogger(__name__)

# Só RS256: um token com "alg: none" ou HS256 (assinado com a chave pública como
# segredo) é recusado antes de qualquer verificação.
ALGORITMOS = ["RS256"]
TOLERANCIA_RELOGIO_S = 30
CACHE_CHAVES_S = 300
INTERVALO_MINIMO_S = 30


class FalhaDeAutorizacao(Exception):
    """O Keycloak recusou a troca do token (sem detalhes: eles ficam no log)."""


class VerificadorJWT:
    """Valida o token de quem chama: assinatura, emissor, audiência e validade.

    Implementa o TokenVerifier do SDK do MCP. As chaves públicas (JWKS) ficam em
    cache; uma chave desconhecida força uma nova busca (o Keycloak trocou a chave),
    mas no máximo uma vez a cada 30 segundos, para um token forjado com "kid"
    aleatório não virar um ataque contra o Keycloak.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        chaves_url: str,
        emissor: str,
        audiencia: str,
        recurso: str,
    ) -> None:
        self._http = http
        self._chaves_url = chaves_url
        self._emissor = emissor
        self._audiencia = audiencia
        self._recurso = recurso
        self._chaves: dict[str, jwt.PyJWK] = {}
        self._buscadas_em = -float("inf")
        self._trava = anyio.Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            chave = await self._chave(kid)
            if chave is None:
                log.info("token recusado: chave desconhecida")
                return None
            claims: dict[str, Any] = jwt.decode(
                token,
                key=chave.key,
                algorithms=ALGORITMOS,
                audience=self._audiencia,
                issuer=self._emissor,
                leeway=TOLERANCIA_RELOGIO_S,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as erro:
            # Nunca o token no log: só o motivo.
            log.info("token recusado: %s", type(erro).__name__)
            return None
        except httpx.HTTPError as erro:
            log.warning("chaves do Keycloak indisponiveis: %s", type(erro).__name__)
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp", "")),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self._recurso,
            subject=str(claims["sub"]),
            claims={k: claims[k] for k in ("iss", "aud", "azp") if k in claims},
        )

    async def _chave(self, kid: object) -> jwt.PyJWK | None:
        if not isinstance(kid, str):
            return None
        async with self._trava:
            idade = time.monotonic() - self._buscadas_em
            # Busca de novo quando o cache vence, ou quando aparece uma chave nova
            # e a última busca não foi há pouco.
            if idade >= CACHE_CHAVES_S or (kid not in self._chaves and idade >= INTERVALO_MINIMO_S):
                try:
                    await self._buscar_chaves()
                except httpx.HTTPError as erro:
                    if not self._chaves:
                        raise
                    # Keycloak fora do ar por um instante: as chaves que já temos
                    # continuam valendo (ele não as descarta de uma hora para outra),
                    # em vez de recusar o token de todo mundo. Nova tentativa em 30 s.
                    log.warning(
                        "chaves do Keycloak indisponiveis (%s): usando as de antes", type(erro).__name__
                    )
                    self._buscadas_em = time.monotonic() - CACHE_CHAVES_S + INTERVALO_MINIMO_S
        return self._chaves.get(kid)

    async def _buscar_chaves(self) -> None:
        resposta = await self._http.get(self._chaves_url)
        resposta.raise_for_status()
        conjunto = jwt.PyJWKSet.from_dict(resposta.json())
        self._chaves = {k.key_id: k for k in conjunto.keys if k.key_id and k.public_key_use in (None, "sig")}
        self._buscadas_em = time.monotonic()


class TrocaDeToken:
    """Troca um token por outro para um serviço específico (RFC 8693, Keycloak 26).

    O token trocado fica em cache até perto de expirar: uma conversa com várias
    chamadas de ferramenta não vira uma troca por chamada.
    """

    GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
    TIPO_ACCESS = "urn:ietf:params:oauth:token-type:access_token"

    def __init__(
        self, http: httpx.AsyncClient, token_url: str, cliente_id: str, cliente_segredo: SecretStr
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._cliente = (cliente_id, cliente_segredo)
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    async def trocar(self, token: str, audiencia: str) -> str:
        chave = (hashlib.sha256(token.encode()).hexdigest(), audiencia)
        guardado = self._cache.get(chave)
        if guardado and guardado[1] > time.time():
            return guardado[0]

        cliente_id, segredo = self._cliente
        try:
            resposta = await self._http.post(
                self._token_url,
                auth=(cliente_id, segredo.get_secret_value()),
                data={
                    "grant_type": self.GRANT,
                    "subject_token": token,
                    "subject_token_type": self.TIPO_ACCESS,
                    "requested_token_type": self.TIPO_ACCESS,
                    "audience": audiencia,
                },
            )
        except httpx.HTTPError as erro:
            log.warning("troca de token indisponivel: %s", type(erro).__name__)
            raise FalhaDeAutorizacao from erro
        if resposta.status_code != httpx.codes.OK:
            # O corpo do erro do Keycloak diz o motivo, sem dados do cliente.
            log.warning("troca de token recusada: %s %s", resposta.status_code, resposta.text[:200])
            raise FalhaDeAutorizacao
        corpo = resposta.json()
        novo = str(corpo["access_token"])
        validade = float(corpo.get("expires_in", 60))
        self._cache[chave] = (novo, time.time() + max(validade - TOLERANCIA_RELOGIO_S, 0))
        self._limpar_expirados()
        return novo

    def _limpar_expirados(self) -> None:
        agora = time.time()
        for chave in [c for c, (_, ate) in self._cache.items() if ate <= agora]:
            del self._cache[chave]
