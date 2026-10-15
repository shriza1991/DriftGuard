from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mock_api.business.router import router as business_router
from mock_api.config import settings

app = FastAPI(
    title="DriftGuard Mock API Service",
    description="Simulated third-party ticketing & user service with configurable drift mutations (Member 1 scope).",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(business_router)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "mock_api",
        "version": "1.0.0"
    }


@app.get("/ready")
async def readiness_check():
    return {"status": "ready", "service": "mock_api"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mock_api.app:app", host=settings.host, port=settings.port, reload=True)
