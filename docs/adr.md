# Decisiones de arquitectura (ADR)

Cada decisión dice qué elegimos, qué descartamos y **qué perdemos**. Formato corto a propósito.

## ADR-0001 · Redirección 302 con `Cache-Control: no-store`
- **Decisión:** 302 (no 301) y sin caché en CloudFront para la ruta `/{código}`.
- **Por qué:** el destino se puede editar y cada visita debe llegar al servidor para contarse y validar caducidad, límite y contraseña.
- **Costo:** cada visita pasa por CloudFront → ALB → EC2. Lo compensamos con la caché en Redis (ADR-0004).

## ADR-0002 · Códigos aleatorios de 7 caracteres, alfabeto de 31 símbolos, insensibles a mayúsculas
- **Decisión:** `23456789abcdefghjkmnpqrstuvwxyz` → 31⁷ ≈ 2.75 × 10¹⁰ códigos. Todo se guarda en minúsculas.
- **Alternativas:** hash MD5 truncado (colisiones + lectura extra), contador global (SPOF y carreras), servicio KGS con claves pre-generadas (Ch12 del libro).
- **Por qué:** con 60 M enlaces en 5 años la probabilidad de colisión por intento es ≈ 2 × 10⁻³; un `PutItem` condicional (`attribute_not_exists(code)`) la detecta y reintentamos. Sin coordinación entre instancias, sin SPOF. Insensible a mayúsculas = alias únicos sin ambigüedad y enlaces que se pueden teclear desde un cartel.
- **Costo:** espacio menor que base62 (57⁷ ≈ 1.9 × 10¹²). Si la tasa de colisión crece, migrar a KGS (trabajo a futuro).

## ADR-0003 · DynamoDB como base de datos
- **Decisión:** DynamoDB on-demand: tabla `links` (PK `code`, GSI disperso `owner-index`), `stats` y `moderation`.
- **Alternativa:** RDS PostgreSQL Multi-AZ.
- **Por qué:** el acceso es clave-valor por código; escala horizontal sin administrar réplicas; TTL nativo para limpiar enlaces caducados; escrituras condicionales atómicas (ADR-0005); costo casi cero cuando no hay tráfico, importante porque encendemos solo para demos.
- **Costo:** sin joins ni consultas ad hoc; la búsqueda del panel se hace sobre los enlaces del usuario (decenas o cientos), no sobre toda la tabla. Lecturas eventualmente consistentes (ver ADR-0004).

## ADR-0004 · Caché cache-aside en Redis (ElastiCache/Valkey Multi-AZ)
- **Decisión:** la redirección lee de Redis; si falla, DynamoDB, y llena la caché. TTL de 300 s + jitter aleatorio.
- **Problemas cubiertos:** *avalanche* (TTL escalonado), *penetration* (caché negativa de 60 s para códigos inexistentes: frena a quien recorre códigos), *hot key* (un enlace viral queda en memoria).
- **Edición casi inmediata:** al editar/desactivar se borra la clave → la siguiente visita lee el valor nuevo.
- **Lectura de lo propio:** al crear un enlace lo escribimos también en la caché, así abrirlo inmediatamente no depende de la consistencia eventual de DynamoDB.
- **Fail-open:** si Redis no responde (timeout 250 ms) seguimos contra DynamoDB. Redis acelera, pero no es requisito para estar disponibles.

## ADR-0005 · Límite de clics exacto con escritura condicional
- **Decisión:** `UpdateItem SET clicks = clicks + 1 IF clicks < max_clicks AND activo AND vigente`.
- **Por qué:** la condición se evalúa de forma atómica en DynamoDB; con 1 000 visitas simultáneas a un enlace con máximo 100 pasan exactamente 100. Un contador en la aplicación o en caché permitiría carreras entre instancias.
- **Costo:** los enlaces con límite hacen una escritura síncrona por visita (≈ 5–10 ms). Los enlaces sin límite cuentan de forma asíncrona.
- **Bots:** los generadores de vista previa (WhatsApp, Slack…) no consumen clics de enlaces con límite: reciben una página neutral sin el destino.

## ADR-0006 · Clics asíncronos con SQS + worker
- **Decisión:** la redirección publica el evento en SQS *después* de responder (background task); un worker agrega por día/país/dispositivo/navegador/SO/origen.
- **Privacidad:** no se guarda la IP. País por la cabecera de CloudFront; visitantes únicos con HMAC(IP + UA) usando una sal que cambia cada día.
- **Costo:** entrega al-menos-una-vez → un reintento puede contar doble en los contadores (los visitantes únicos son un conjunto, idempotente). Aceptable para métricas.

## ADR-0007 · ALB administrado en lugar de HAProxy en EC2
- **Decisión:** Application Load Balancer en dos AZ, algoritmo *least outstanding requests*, health checks cada 10 s.
- **Por qué:** el laboratorio de HAProxy en una VM era un SPOF; el ALB es redundante por diseño y se integra con el ASG.
- **Costo:** menos control fino que `haproxy.cfg`; ~16 USD/mes fijos.

## ADR-0008 · Un solo NAT Gateway
- **Decisión:** 1 NAT en una AZ. DynamoDB va por un VPC endpoint de gateway (gratis), no por el NAT.
- **Por qué:** cada NAT cuesta ~32 USD/mes. La ruta crítica (Redis + DynamoDB) no usa el NAT.
- **Costo:** si cae la AZ del NAT, se pierden temporalmente la publicación de clics (SQS) y Safe Browsing; las redirecciones siguen. En producción: un NAT por AZ o endpoints de interfaz para SQS.

## ADR-0009 · Seguridad por capas
- WAF en CloudFront (reputación de IPs, OWASP común, rate limit global y de creación).
- El ALB solo acepta tráfico del prefijo de CloudFront **y** con la cabecera secreta `X-Origin-Verify`.
- SGs encadenados ALB → EC2 → Redis; instancias en subredes privadas, sin SSH (Session Manager), IMDSv2.
- TLS 1.2+ de punta a punta (visitante → CloudFront → ALB → Redis). Cifrado en reposo en DynamoDB, Redis, SQS, S3 y EBS.
- Cognito (código + PKCE) con tokens cortos; autorización a nivel de objeto (solo el dueño ve o edita su enlace; 404 en lugar de 403 para no revelar qué códigos existen).
- Contraseñas de enlaces con argon2 y límite de intentos.
- Tope de 6 instancias en el ASG: limita cuánto puede costar un ataque de tráfico.

## ADR-0010 · Encender solo para usar
- **Decisión:** toda la infraestructura en Terraform; `deploy.sh` la crea en ~15 min y `deploy.sh destroy` la borra.
- **Costo encendido:** ≈ 0.15 USD/h (ALB, 2 × t3.small, NAT, 2 nodos de caché, WAF). Apagado: ≈ 0 (zona de Route 53 0.50 USD/mes).
- **Costo:** se pierden los datos al destruir. Para conservarlos, dejar las tablas fuera del destroy o exportarlas.
