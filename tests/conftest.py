"""Auxiliares dos testes: um "Keycloak de mentira" que emite tokens assinados de verdade."""

import time
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

EMISSOR = "http://keycloak.teste/realms/bazar"
CHAVES_URL = f"{EMISSOR}/protocol/openid-connect/certs"
TOKEN_URL = f"{EMISSOR}/protocol/openid-connect/token"
URL_MCP = "http://mcp.teste/mcp"
API_URL = "http://bazar.teste"

EmitirToken = Callable[..., str]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def chave_privada() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(chave_privada: rsa.RSAPrivateKey) -> dict[str, Any]:
    publica = jwt.algorithms.RSAAlgorithm.to_jwk(chave_privada.public_key(), as_dict=True)
    return {"keys": [{**publica, "kid": "chave-1", "use": "sig", "alg": "RS256"}]}


@pytest.fixture
def emitir_token(chave_privada: rsa.RSAPrivateKey) -> EmitirToken:
    def emitir(
        *,
        aud: str | list[str] = "bazar-mcp",
        iss: str = EMISSOR,
        sub: str = "cliente-1",
        expira_em: int = 300,
        kid: str = "chave-1",
        chave: rsa.RSAPrivateKey | None = None,
        **extras: Any,
    ) -> str:
        agora = int(time.time())
        claims = {
            "iss": iss,
            "aud": aud,
            "sub": sub,
            "iat": agora,
            "exp": agora + expira_em,
            "azp": "bazar-agente",
            "scope": "openid email",
            **extras,
        }
        return jwt.encode(claims, chave or chave_privada, algorithm="RS256", headers={"kid": kid})

    return emitir
