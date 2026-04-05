import os
import logging
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

# Try importing GCP Trace Exporter, fallback to Console if not available or not in GCP
try:
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    HAS_GCP_EXPORTER = True
except ImportError:
    HAS_GCP_EXPORTER = False

logger = logging.getLogger(__name__)

def setup_telemetry(app: FastAPI, engine=None):
    """
    Initializes OpenTelemetry tracing for the FastAPI application and SQLAlchemy engine.
    If running in GCP (or with GCP credentials), it exports to Google Cloud Trace.
    """
    # 1. Set up the Tracer Provider
    provider = TracerProvider()
    trace.set_tracer_provider(provider)

    # 2. Configure the Exporter
    if HAS_GCP_EXPORTER and os.getenv("GCP_PROJECT_ID") and os.getenv("ENABLE_CLOUD_TRACE", "").lower() == "true":
        try:
            exporter = CloudTraceSpanExporter(project_id=os.getenv("GCP_PROJECT_ID"))
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            logger.info("OpenTelemetry configured with GCP Cloud Trace Exporter.")
        except Exception as e:
            logger.warning(f"Failed to initialize GCP Cloud Trace Exporter: {e}")
    else:
        logger.info("OpenTelemetry configured without an exporter (traces will not be sent).")

    # 3. Instrument FastAPI
    FastAPIInstrumentor.instrument_app(app)

    # 4. Instrument SQLAlchemy (if an engine is provided)
    if engine:
        try:
            # SQLAlchemyInstrumentor expects the sync engine
            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
            logger.info("OpenTelemetry configured for SQLAlchemy.")
        except Exception as e:
            logger.warning(f"Failed to instrument SQLAlchemy: {e}")
