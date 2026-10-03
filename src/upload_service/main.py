from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from upload_service.api.routes.health import router as health_router
from upload_service.api.routes.uploads import router as uploads_router
from upload_service.config import settings
from upload_service.telemetry import setup_telemetry

app = FastAPI(title=settings.service_name)

setup_telemetry(app)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(health_router)
app.include_router(uploads_router)
