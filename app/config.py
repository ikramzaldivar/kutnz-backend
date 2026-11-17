from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración por variables de entorno (12-factor). Prefijo KUT_."""

    model_config = SettingsConfigDict(env_prefix="KUT_", env_file=".env", extra="ignore")

    # Dominio público de los enlaces cortos
    public_host: str = "kut.nz"
    public_scheme: str = "https"
    # Hosts propios adicionales que nunca pueden ser destino (p. ej. el dominio de CloudFront)
    extra_self_hosts: str = ""

    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None  # DynamoDB Local / ElasticMQ en desarrollo

    table_links: str = "kutnz-links"
    table_stats: str = "kutnz-stats"
    table_moderation: str = "kutnz-moderation"
    clicks_queue_url: str = ""

    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 300
    negative_cache_ttl_seconds: int = 60

    # Límites de abuso
    anon_creates_per_hour: int = 10
    user_creates_per_hour: int = 200
    password_attempts_per_15min: int = 5

    # Autenticación: "cognito" en AWS, "dev" en local (Bearer dev:<usuario>[:admin])
    auth_mode: str = "dev"
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""
    cognito_domain: str = ""

    safe_browsing_api_key: str = ""
    # Secreto para derivar la sal diaria del identificador anónimo de visitantes
    visitor_salt_secret: str = "change-me"

    # 'none' en producción (anti-clickjacking); el servidor local usa 'self' para revisar vistas móviles en iframes.
    frame_ancestors: str = "'none'"

    serve_static_locally: bool = True
    static_base_url: str = "/static"

    @property
    def base_url(self) -> str:
        return f"{self.public_scheme}://{self.public_host}"

    @property
    def self_hosts(self) -> set[str]:
        hosts = {self.public_host.lower(), f"www.{self.public_host.lower()}"}
        hosts |= {h.strip().lower() for h in self.extra_self_hosts.split(",") if h.strip()}
        return hosts


@lru_cache
def get_settings() -> Settings:
    return Settings()
