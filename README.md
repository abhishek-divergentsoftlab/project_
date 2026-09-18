# AI-Powered B2B Marketplace

This repository contains an AI-powered B2B marketplace platform for buyers and sellers, featuring a chat-based UI that parses streaming events from the AI backend and a vector database for semantic product searches.

## Project Structure

- `frontend/`: React + Vite application (TypeScript, Vanilla CSS with Glassmorphism)
- `backend/`: FastAPI application (Python, LangGraph, Qdrant)

## Prerequisites

- **Node.js** (v18+ recommended)
- **Python 3.9+**
- **Ollama** (optional, required if generating real embeddings/LLM responses instead of mocked data)

---

## 🚀 Getting Started

### 1. Backend Setup

The backend uses FastAPI and an in-memory instance of Qdrant for semantic search.

Open a new terminal and navigate to the backend directory:

```bash
cd backend
```

**Set up the virtual environment & install dependencies:**
```bash
# Activate virtual environment
source .venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

**Generate Mock Data:**
We provide a script to seed the in-memory Qdrant database with fake buyers (looking for processors/cables) and sellers (offering markers/cables).
```bash
python scripts/generate_fake_data.py
```

**Start the Backend Server:**
```bash
uvicorn app:app --reload --port 8000
```
The API will be available at http://localhost:8000.

### 2. Frontend Setup

The frontend provides the chat UI that handles Server-Sent Events (SSE) streaming for the AI agent.

Open a new terminal and navigate to the frontend directory:

```bash
cd frontend
```

**Install dependencies:**
```bash
npm install
```

**Start the Frontend Development Server:**
```bash
npm run dev
```
The web app will typically be available at http://localhost:5173 (check your terminal for the exact URL).

---

## Features

- **Agentic Chat UI:** Parses streaming JSON chunks to dynamically display "Thinking..." states, tool usage (checkmarks), and final responses.
- **Onboarding Flow:** Structured data extraction for buyers and sellers (mocked).
- **Semantic Search:** Qdrant-based vector search for finding relevant RFQs (Request for Quotations) and products based on user prompts.
- **Authentication:** Standard JWT-based login/signup flow implementation.

## Future Improvements

- Connect local Ollama server to LangGraph workflow to replace the current mock stream.
- Expand Qdrant setup to use Docker instead of in-memory mode for persistent data storage.
- Integrate a real email provider API for user communication tool.
