# InvGate Dashboard

Dashboard de tickets para InvGate Service Management.
Stack: Python extractor + SQLite + Flask API + Nginx + Docker Compose.

## Estructura

```
invgate-dashboard/
├── docker-compose.yml
├── .env.example          <- copiar a .env y completar
├── extractor/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── extract.py        <- extractor incremental
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── api.py            <- Flask /api/metrics
└── nginx/
    ├── Dockerfile
    ├── nginx.conf
    └── html/
        └── index.html    <- dashboard
```

## Deploy

```bash
# 1. Clonar / copiar el proyecto
cd invgate-dashboard

# 2. Configurar credenciales
cp .env.example .env
nano .env    # completar INVGATE_USER y INVGATE_PASS

# 3. Levantar
docker compose up -d --build

# 4. Ver logs de la carga inicial (puede tardar 1-2 min)
docker compose logs -f extractor

# 5. Abrir dashboard
http://<IP-VM>:8080
```

## Comandos utiles

```bash
# Ver estado de servicios
docker compose ps

# Logs en tiempo real
docker compose logs -f

# Forzar recarga completa (rehace toda la DB)
docker compose exec extractor python extract.py --full

# Conectar a SQLite directamente
docker compose exec extractor sqlite3 /data/invgate.db

# Reiniciar solo la API
docker compose restart api

# Detener todo
docker compose down

# Detener y borrar DB (carga desde cero al volver a levantar)
docker compose down -v
```

## Queries SQLite utiles

```sql
-- Tickets por estado
SELECT l.name, COUNT(*) FROM tickets t
JOIN lookups l ON l.entity='status' AND l.id=CAST(t.status_id AS TEXT)
GROUP BY 1 ORDER BY 2 DESC;

-- Tiempo promedio de resolucion en horas (ultimos 30 dias)
SELECT ROUND(AVG((COALESCE(solved_at, closed_at) - created_at) / 3600.0), 1) AS avg_hours
FROM tickets
WHERE (solved_at IS NOT NULL OR closed_at IS NOT NULL)
  AND created_at > strftime('%s', 'now', '-30 days');

-- Top 10 categorias abiertas
SELECT l.name, COUNT(*) FROM tickets t
JOIN lookups l ON l.entity='category' AND l.id=CAST(t.category_id AS TEXT)
WHERE t.status_id NOT IN (6, 7, 8)   -- ajustar segun tus status IDs
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

## Ajustes

- **Frecuencia de extraccion**: cambiar `CRON_SCHEDULE` en `.env`
- **Hilos paralelos**: cambiar `MAX_WORKERS` en `.env` (default 10)
- **Puerto del dashboard**: cambiar `DASHBOARD_PORT` en `.env` (default 8080)
