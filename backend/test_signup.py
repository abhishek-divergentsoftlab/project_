from fastapi.testclient import TestClient
from app import app
import traceback

client = TestClient(app, raise_server_exceptions=True)
try:
    response = client.post("/api/v1/auth/signup", json={"email": "test11@example.com", "password": "test"})
    print("Status:", response.status_code)
except Exception as e:
    traceback.print_exc()
print("Response:", response.text)
