from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from orchestrator.api.routes import router as orchestrator_router
from orchestrator.config import settings

app = FastAPI(
    title="DriftGuard Orchestrator Service",
    description="Autonomous Tool Registry and Agent Workflow Engine (Member 2 Scope).",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(orchestrator_router)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "orchestrator",
        "mock_api_url": settings.mock_api_url,
        "model_provider": settings.model_provider,
        "version": "1.0.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("orchestrator.app:app", host=settings.host, port=settings.port, reload=True)
