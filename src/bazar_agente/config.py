"""Configuração por variáveis de ambiente: nenhum endereço fixo nem segredo no código."""

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings


class _ComKeycloak(BaseSettings):
    # O "iss" dos tokens (endereço público) e, se diferente, o endereço interno
    # pelo qual os containers falam com o Keycloak.
    emissor: AnyHttpUrl = AnyHttpUrl("http://localhost:8180/realms/bazar")
    keycloak_interno: AnyHttpUrl | None = None

    @property
    def _keycloak(self) -> str:
        return str(self.keycloak_interno or self.emissor).rstrip("/")

    @property
    def chaves_url(self) -> str:
        return f"{self._keycloak}/protocol/openid-connect/certs"

    @property
    def token_url(self) -> str:
        return f"{self._keycloak}/protocol/openid-connect/token"


class ConfigMCP(_ComKeycloak, env_prefix="BAZAR_MCP_", extra="ignore"):
    """Servidor MCP do Bazar. Variáveis com o prefixo BAZAR_MCP_ (por exemplo, BAZAR_MCP_API_URL)."""

    # Gateway do Bazar: todas as chamadas passam por ele, que confere o token de novo.
    api_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8080")
    # Endereço público do endpoint MCP. Pela especificação, é o identificador do
    # recurso (RFC 8707/9728): vai nos metadados OAuth e nos tokens aceitos.
    url_publica: AnyHttpUrl = AnyHttpUrl("http://localhost:8090/mcp")

    # Este servidor só aceita tokens emitidos para ele (aud)...
    audiencia: str = "bazar-mcp"
    # ...e, para chamar o Bazar, troca o token por outro com aud = bazar-api (RFC 8693).
    audiencia_api: str = "bazar-api"
    cliente_id: str = "bazar-mcp"
    cliente_segredo: SecretStr = SecretStr("")

    # Hosts aceitos no cabeçalho Host (proteção contra DNS rebinding).
    hosts_permitidos: list[str] = ["localhost:8090", "127.0.0.1:8090"]


class ConfigAgente(_ComKeycloak, env_prefix="BAZAR_AGENTE_", extra="ignore"):
    """API do agente. Variáveis com o prefixo BAZAR_AGENTE_ (por exemplo, BAZAR_AGENTE_MODELO)."""

    # Servidor MCP do Bazar (endpoint Streamable HTTP).
    mcp_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8090/mcp")
    # O agente aceita só tokens emitidos para ele e troca por um do servidor MCP.
    audiencia: str = "bazar-agente"
    audiencia_mcp: str = "bazar-mcp"
    cliente_id: str = "bazar-agente"
    cliente_segredo: SecretStr = SecretStr("")

    # Modelo de linguagem: "ollama" (local, gratuito) ou "anthropic" (com chave).
    provedor: str = "ollama"
    modelo: str = "qwen3:4b"
    ollama_url: AnyHttpUrl = AnyHttpUrl("http://localhost:11434")
    anthropic_chave: SecretStr = SecretStr("")

    # Endereço do painel do Bazar, a única origem que o navegador pode usar.
    origens_cors: list[str] = ["http://localhost:5173"]
    # Limites de uso por cliente.
    mensagens_por_minuto: int = 10
    tamanho_maximo_mensagem: int = 2000
    # Cada etapa do grafo (uma chamada ao modelo ou uma rodada de ferramentas) não
    # passa disto. Generoso porque, sem placa de vídeo, o modelo leva minutos.
    tempo_maximo_etapa_s: float = 600
