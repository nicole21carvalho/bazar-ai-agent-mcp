"""Configuração por variáveis de ambiente: nenhum endereço fixo nem segredo no código."""

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigMCP(BaseSettings):
    """Servidor MCP do Bazar. Variáveis com o prefixo BAZAR_MCP_ (por exemplo, BAZAR_MCP_API_URL)."""

    model_config = SettingsConfigDict(env_prefix="BAZAR_MCP_", extra="ignore")

    # Gateway do Bazar: todas as chamadas passam por ele, que confere o token de novo.
    api_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8080")
    # Endereço público do endpoint MCP. Pela especificação, é o identificador do
    # recurso (RFC 8707/9728): vai nos metadados OAuth e nos tokens aceitos.
    url_publica: AnyHttpUrl = AnyHttpUrl("http://localhost:8090/mcp")
    # Keycloak: o "iss" dos tokens e o endereço interno para buscar chaves e trocar tokens.
    emissor: AnyHttpUrl = AnyHttpUrl("http://localhost:8180/realms/bazar")
    keycloak_interno: AnyHttpUrl | None = None

    # Este servidor só aceita tokens emitidos para ele (aud)...
    audiencia: str = "bazar-mcp"
    # ...e, para chamar o Bazar, troca o token por outro com aud = bazar-api (RFC 8693).
    audiencia_api: str = "bazar-api"
    cliente_id: str = "bazar-mcp"
    cliente_segredo: SecretStr = SecretStr("")

    # Hosts aceitos no cabeçalho Host (proteção contra DNS rebinding).
    hosts_permitidos: list[str] = ["localhost:8090", "127.0.0.1:8090"]

    @property
    def _keycloak(self) -> str:
        return str(self.keycloak_interno or self.emissor).rstrip("/")

    @property
    def chaves_url(self) -> str:
        return f"{self._keycloak}/protocol/openid-connect/certs"

    @property
    def token_url(self) -> str:
        return f"{self._keycloak}/protocol/openid-connect/token"
