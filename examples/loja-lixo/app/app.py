"""Fictitious shop backend (example for ExitPilot)."""
import os

import sentry_sdk
import stripe
from flask import Flask

sentry_sdk.init()  # reads SENTRY_DSN from the environment
stripe.api_key = os.environ["STRIPE_SECRET"]
UPLOADS_URL = os.getenv("S3_BUCKET_URL")

app = Flask(__name__)


@app.post("/checkout")
def checkout():
    session = stripe.checkout.Session.create(mode="payment", line_items=[])
    return {"id": session.id, "uploads": UPLOADS_URL}
