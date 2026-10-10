"""API Gateway (HTTP API) -> FastAPI via Mangum."""

from mangum import Mangum

from app.api.main import app

handler = Mangum(app, lifespan="off")
