"""
Visitor Management System API
Main application file
"""

from fastapi import FastAPI, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import logging
import uvicorn

from app.core.config import settings
from app.core.database import engine, Base, get_db
from app.routers import approver, visitor, icard, sms_webhook, appointment
# from app.models import Approver, Visitor

# ============================================================================
# Logging Configuration
# ============================================================================

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# FastAPI Application
# ============================================================================

# Disable docs in production for Lambda compatibility
docs_url = "/docs" if settings.ENVIRONMENT != "production" else None
redoc_url = "/redoc" if settings.ENVIRONMENT != "production" else None

app = FastAPI(
    title="Visitor Management System API",
    version="1.0.0",
    description="A comprehensive visitor management system with approver authentication and visitor check-in tracking",
    contact={
        "name": "API Support",
        "email": "support@example.com",
    },
    license_info={
        "name": "Proprietary",
    },
    docs_url=docs_url,
    redoc_url=redoc_url,
)

# ============================================================================
# CORS Configuration
# ============================================================================

if settings.API_CORS_ORIGINS and settings.API_CORS_ORIGINS.strip() == "*":
    # Allow all origins (credentials must be False)
    origins = ["*"]
    allow_credentials = False
else:
    # Specific origins (credentials can be True)
    origins = [
        "http://localhost:3000",
        "http://localhost:4000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:4000",
        "http://127.0.0.1:4001",
        "https://x5xqkl8w-4000.inc1.devtunnels.ms",
        "https://q80bvqq1-3000.inc1.devtunnels.ms",
        # Add your tunnel URLs here
    ]

    if settings.API_CORS_ORIGINS:
        additional_origins = [o.strip() for o in settings.API_CORS_ORIGINS.split(",") if o.strip()]
        origins.extend(additional_origins)

    allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],  # Allow all methods including OPTIONS for preflight
    allow_headers=["*"],  # Allow all headers
    expose_headers=["*"],  # Expose all headers in response
)

# ============================================================================
# Root & Health Endpoints
# ============================================================================

@app.get("/", tags=["Root"])
def root():
    """Root endpoint - API information"""
    return {
        "name": "Visitor Management System API",
        "version": "1.0.0",
        "status": "running",
        "documentation": "/docs",
        "endpoints": {
            "health": "/health",
            "approvers": "/api/approvers",
            "visitors": "/api/visitors",
            "icards": "/api/icards"
        }
    }

@app.get("/health", tags=["Health"])
def health_check():
    """Health check endpoint for API monitoring"""
    try:
        return {
            "status": "ok",
            "message": "Visitor Management System API is running",
            "version": "1.0.0",
            "environment": getattr(settings, 'ENVIRONMENT', 'unknown'),
            "timestamp": str(__import__('datetime').datetime.utcnow())
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Health check failed: {str(e)}",
            "version": "1.0.0"
        }

@app.get("/api/health", tags=["Health"])
def api_health():
    """Health check endpoint (alternative path)"""
    try:
        return {
            "status": "ok",
            "message": "Visitor Management System API is running",
            "version": "1.0.0",
            "environment": getattr(settings, 'ENVIRONMENT', 'unknown')
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"API health check failed: {str(e)}",
            "version": "1.0.0"
        }

# ============================================================================
# Event Handlers
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Actions to perform on application startup"""
    logger.info("=" * 60)
    logger.info("Starting Visitor Management System API")
    logger.info("=" * 60)
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Database: {settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")
    logger.info(f"CORS Origins: {settings.API_CORS_ORIGINS or 'Default'}")
    logger.info(f"JWT Expiration: {settings.JWT_EXPIRATION_HOURS} hours")
    logger.info("=" * 60)

    # Create database tables if they don't exist
    try:
        logger.info("Ensuring database tables exist...")
        Base.metadata.create_all(bind=engine)
        logger.info("✓ Database tables ready")
    except Exception as e:
        logger.error(f"✗ Database initialization failed: {e}")
        logger.warning("Application will continue, but database operations may fail")

@app.on_event("shutdown")
async def shutdown_event():
    """Actions to perform on application shutdown"""
    logger.info("=" * 60)
    logger.info("Shutting down Visitor Management System API")
    logger.info("=" * 60)

# ============================================================================
# Router Registration
# ============================================================================

logger.info("Registering API routers...")
app.include_router(approver.router)  # Approver authentication and management
app.include_router(visitor.router)  # Visitor check-in and management
app.include_router(icard.router)  # ICard management
app.include_router(sms_webhook.router)  # SMS webhook for reply handling
app.include_router(appointment.router)  # Appointment management

# Add Google Form endpoint at root level to match Apps Script URL
from app.routers.visitor import google_form_submission, GoogleFormSubmission

@app.post("/api/google-form", status_code=201)
def google_form_root_endpoint(
    form_data: GoogleFormSubmission,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """Root level Google Form endpoint to match Apps Script URL"""
    return google_form_submission(form_data, background_tasks, db)

logger.info("All routers registered successfully")

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=settings.LOG_LEVEL.lower()
    )
