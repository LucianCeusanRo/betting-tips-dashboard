"""Entry point: read-only live monitor (server.py) + admin console routes (admin_routes.py).

Run from the repository root:  uvicorn live_dashboard.main:app --host 127.0.0.1 --port 8765
"""
from live_dashboard.server import app
from live_dashboard.admin_routes import router as admin_router

app.include_router(admin_router)
