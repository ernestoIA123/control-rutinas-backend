import os
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

APP_NAME = "Control de rutinas"
PLAN_NAME = "Plan mensual: control de rutinas"

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
PRICE_ID = os.getenv("PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
APP_URL = os.getenv("APP_URL", "http://localhost:5173")
LOGIN_URL = os.getenv("LOGIN_URL", APP_URL)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "netooficial200@gmail.com")

if not SUPABASE_URL:
    raise RuntimeError("Falta SUPABASE_URL")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Falta SUPABASE_SERVICE_ROLE_KEY")
if not STRIPE_SECRET_KEY:
    raise RuntimeError("Falta STRIPE_SECRET_KEY")
if not PRICE_ID:
    raise RuntimeError("Falta PRICE_ID")

stripe.api_key = STRIPE_SECRET_KEY
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI(title="Control de rutinas backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CheckoutRequest(BaseModel):
    email: str

class ValidateAccessRequest(BaseModel):
    email: str

class ActivateUserRequest(BaseModel):
    email: str
    access_active: bool
    subscription_status: Optional[str] = None
    plan: Optional[str] = None
    current_period_end: Optional[str] = None

def get_user_by_email(email: str):
    email = email.strip().lower()
    result = (
        supabase.table("usuarios")
        .select("*")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    data = result.data or []
    return data[0] if data else None

def upsert_user_access(
    email: str,
    access_active: bool,
    subscription_status: str,
    plan: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    current_period_end: Optional[str] = None,
):
    email = email.strip().lower()
    payload = {
        "email": email,
        "access_active": access_active,
        "subscription_status": subscription_status,
        "plan": plan,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "current_period_end": current_period_end,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    existing = get_user_by_email(email)
    if not existing:
        return None
    result = (
        supabase.table("usuarios")
        .update(payload)
        .eq("email", email)
        .execute()
    )
    return result.data

def to_iso_from_unix(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

@app.get("/")
def root():
    return {
        "ok": True,
        "message": "Backend de Control de rutinas funcionando",
        "app": APP_NAME,
        "plan": PLAN_NAME,
    }

@app.get("/health")
def health():
    return {"ok": True, "service": "healthy"}

@app.post("/create-checkout")
def create_checkout(body: CheckoutRequest):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            customer_email=email,
            success_url=f"{APP_URL}?checkout=success",
            cancel_url=f"{APP_URL}?checkout=cancel",
            metadata={
                "email": email,
                "app_name": APP_NAME,
                "plan_name": PLAN_NAME,
            },
        )
        return {
            "ok": True,
            "checkout_url": session.url,
            "session_id": session.id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando checkout: {str(e)}")

@app.post("/validate-access")
def validate_access(body: ValidateAccessRequest):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")
    user = get_user_by_email(email)
    if not user:
        return {
            "ok": True,
            "exists": False,
            "access_active": False,
            "should_show_paywall": True,
            "message": "Usuario no encontrado en tabla usuarios",
        }
    access_active = bool(user.get("access_active", False))
    subscription_status = user.get("subscription_status", "inactive")
    plan = user.get("plan", "free")
    return {
        "ok": True,
        "exists": True,
        "access_active": access_active,
        "subscription_status": subscription_status,
        "plan": plan,
        "should_show_paywall": not access_active,
    }

@app.post("/activate-user")
def activate_user(body: ActivateUserRequest):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")
    user = get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    payload = {
        "access_active": body.access_active,
        "subscription_status": body.subscription_status or ("active" if body.access_active else "inactive"),
        "plan": body.plan or PLAN_NAME,
        "current_period_end": body.current_period_end,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = (
        supabase.table("usuarios")
        .update(payload)
        .eq("email", email)
        .execute()
    )
    return {"ok": True, "data": result.data}

@app.post("/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Falta STRIPE_WEBHOOK_SECRET")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Falta stripe-signature")
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload inválido")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma inválida")

    event_type = event["type"]
    data_object = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            email = (
                data_object.get("customer_details", {}).get("email")
                or data_object.get("customer_email")
                or data_object.get("metadata", {}).get("email")
            )
            subscription_id = data_object.get("subscription")
            customer_id = data_object.get("customer")
            if email:
                upsert_user_access(
                    email=email,
                    access_active=True,
                    subscription_status="active",
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                )

        elif event_type == "customer.subscription.updated":
            email = None
            customer_id = data_object.get("customer")
            subscription_id = data_object.get("id")
            status = data_object.get("status", "inactive")
            current_period_end = to_iso_from_unix(data_object.get("current_period_end"))
            if customer_id:
                customer = stripe.Customer.retrieve(customer_id)
                email = customer.get("email")
            if email:
                upsert_user_access(
                    email=email,
                    access_active=(status == "active"),
                    subscription_status=status,
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_end=current_period_end,
                )

        elif event_type == "customer.subscription.deleted":
            email = None
            customer_id = data_object.get("customer")
            subscription_id = data_object.get("id")
            if customer_id:
                customer = stripe.Customer.retrieve(customer_id)
                email = customer.get("email")
            if email:
                upsert_user_access(
                    email=email,
                    access_active=False,
                    subscription_status="canceled",
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_end=None,
                )

        elif event_type == "invoice.payment_failed":
            email = None
            customer_id = data_object.get("customer")
            if customer_id:
                customer = stripe.Customer.retrieve(customer_id)
                email = customer.get("email")
            if email:
                upsert_user_access(
                    email=email,
                    access_active=False,
                    subscription_status="payment_failed",
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                )

        return {"ok": True, "received": True, "event_type": event_type}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando webhook: {str(e)}")