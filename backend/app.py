from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.v1.auth import router as auth_router
from api.v1.chat import router as chat_router

app = FastAPI(title="B2B Marketplace API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(chat_router, prefix="/api/v1", tags=["chat"])

@app.get("/api/v1/health")
def health_check():
    return {"status": "ok"}
