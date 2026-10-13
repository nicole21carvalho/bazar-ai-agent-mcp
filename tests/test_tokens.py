"""Validação de tokens e troca de token (RFC 8693)."""

from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from bazar_agente.seguranca.tokens import FalhaDeAutorizacao, TrocaDeToken, VerificadorJWT
from tests.conftest import CHAVES_URL, EMISSOR, TOKEN_URL, URL_MCP, EmitirToken

pytestmark = pytest.mark.anyio


@pytest.fixture
def verificador() -> VerificadorJWT:
    return VerificadorJWT(
        httpx.AsyncClient(), chaves_url=CHAVES_URL, emissor=EMISSOR, audiencia="bazar-mcp", recurso=URL_MCP
    )


@respx.mock
async def test_token_valido_vira_acesso_com_o_cliente_e_o_recurso(
    verificador: VerificadorJWT, emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    acesso = await verificador.verify_token(emitir_token())
    assert acesso is not None
    assert acesso.subject == "cliente-1"
    assert acesso.resource == URL_MCP
    assert acesso.client_id == "bazar-agente"
    assert acesso.scopes == ["openid", "email"]


@respx.mock
@pytest.mark.parametrize(
    ("caso", "argumentos"),
    [
        ("emitido para outro servico", {"aud": "bazar-api"}),
        ("de outro emissor", {"iss": "http://outro.teste/realms/bazar"}),
        ("expirado", {"expira_em": -120}),
    ],
)
async def test_token_recusado(
    verificador: VerificadorJWT,
    emitir_token: EmitirToken,
    jwks: dict[str, Any],
    caso: str,
    argumentos: dict[str, Any],
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    assert await verificador.verify_token(emitir_token(**argumentos)) is None, caso


@respx.mock
async def test_token_assinado_por_outra_chave_e_recusado(
    verificador: VerificadorJWT, emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    impostora = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await verificador.verify_token(emitir_token(chave=impostora)) is None


@respx.mock
async def test_alg_none_e_hs256_sao_recusados(verificador: VerificadorJWT, jwks: dict[str, Any]) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    claims = {"iss": EMISSOR, "aud": "bazar-mcp", "sub": "x", "iat": 0, "exp": 9999999999}
    sem_assinatura = jwt.encode(claims, key="", algorithm="none", headers={"kid": "chave-1"})
    hs256 = jwt.encode(
        claims, key="segredo-qualquer-com-32-bytes-ou-mais!", algorithm="HS256", headers={"kid": "chave-1"}
    )
    assert await verificador.verify_token(sem_assinatura) is None
    assert await verificador.verify_token(hs256) is None


@respx.mock
async def test_kid_desconhecido_nao_vira_enxurrada_de_buscas_no_keycloak(
    verificador: VerificadorJWT, emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    rota = respx.get(CHAVES_URL).respond(json=jwks)
    for i in range(20):
        assert await verificador.verify_token(emitir_token(kid=f"inventado-{i}")) is None
    assert rota.call_count == 1


@respx.mock
async def test_keycloak_fora_do_ar_recusa_sem_quebrar(
    verificador: VerificadorJWT, emitir_token: EmitirToken
) -> None:
    respx.get(CHAVES_URL).mock(side_effect=httpx.ConnectError("fora do ar"))
    assert await verificador.verify_token(emitir_token()) is None


@pytest.fixture
def troca() -> TrocaDeToken:
    return TrocaDeToken(httpx.AsyncClient(), TOKEN_URL, "bazar-mcp", SecretStr("segredo"))


@respx.mock
async def test_troca_pede_ao_keycloak_um_token_so_para_a_api(troca: TrocaDeToken) -> None:
    rota = respx.post(TOKEN_URL).respond(json={"access_token": "token-da-api", "expires_in": 300})
    assert await troca.trocar("token-do-mcp", "bazar-api") == "token-da-api"

    pedido = rota.calls.last.request
    form = {k: v[0] for k, v in parse_qs(pedido.content.decode()).items()}
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": "token-do-mcp",
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "bazar-api",
    }
    assert pedido.headers["Authorization"].startswith("Basic ")  # o servidor se identifica


@respx.mock
async def test_troca_fica_em_cache_ate_perto_de_expirar(troca: TrocaDeToken) -> None:
    rota = respx.post(TOKEN_URL).respond(json={"access_token": "token-da-api", "expires_in": 300})
    for _ in range(5):
        await troca.trocar("token-do-mcp", "bazar-api")
    assert rota.call_count == 1
    await troca.trocar("token-de-outro-cliente", "bazar-api")
    assert rota.call_count == 2


@respx.mock
async def test_troca_recusada_pelo_keycloak_vira_falha_de_autorizacao(troca: TrocaDeToken) -> None:
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_request"})
    with pytest.raises(FalhaDeAutorizacao):
        await troca.trocar("token-do-mcp", "bazar-api")
