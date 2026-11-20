"""Fictitious online shop (example input for ExitPilot, not meant to run)."""

import os

import requests
import sentry_sdk
import stripe
from flask import Flask, render_template
from openai import OpenAI

sentry_sdk.init(dsn=os.environ["SENTRY_DSN"])
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

app = Flask(__name__)
recommender = OpenAI()

# Product descriptions are written by a European model.
# Docs: https://docs.mistral.ai/api  (a URL in a comment is not a dependency)
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


@app.get("/")
def home():
    return render_template("base.html")


@app.post("/describe/<int:product_id>")
def describe(product_id: int):
    response = requests.post(
        MISTRAL_URL,
        headers={"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}"},
        json={"model": "mistral-small-latest",
              "messages": [{"role": "user", "content": f"Describe product {product_id}"}]},
        timeout=30,
    )
    return response.json()


@app.post("/ship/<int:order_id>")
def ship(order_id: int):
    return requests.post(f"{os.environ['SHIPPING_API_URL']}/shipments", json={"order": order_id}, timeout=10).json()
