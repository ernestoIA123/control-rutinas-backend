import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
from uuid import uuid4

import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import Client, create_client

APP_NAME = "Control de rutinas"
PLAN_NAME = "Plan mensual: control de rutinas"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
PRICE_ID = os.getenv("PRICE_ID", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
APP_URL = os.getenv("APP_URL", "").strip()
LOGIN_URL = os.getenv("LOGIN_URL", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "netooficial200@gmail.com").strip().lower()

if not SUPABASE_URL:
    raise RuntimeError("Falta SUPABASE_URL")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Falta SUPABASE_SERVICE_ROLE_KEY")
if not STRIPE_SECRET_KEY:
    raise RuntimeError("Falta STRIPE_SECRET_KEY")
if not PRICE_ID:
    raise RuntimeError("Falta PRICE_ID")
if not STRIPE_WEBHOOK_SECRET:
    raise RuntimeError("Falta STRIPE_WEBHOOK_SECRET")
if not APP_URL:
    raise RuntimeError("Falta APP_URL")
if not LOGIN_URL:
    raise RuntimeError("Falta LOGIN_URL")

stripe.api_key = STRIPE_SECRET_KEY
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI(title="Control de rutinas backend")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_iso_from_unix(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_allowed_origins() -> list[str]:
    origins = {
        APP_URL.rstrip("/"),
        LOGIN_URL.rstrip("/"),
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    }

    parsed_app = urlparse(APP_URL)
    parsed_login = urlparse(LOGIN_URL)

    if parsed_app.scheme and parsed_app.netloc:
        origins.add(f"{parsed_app.scheme}://{parsed_app.netloc}")
    if parsed_login.scheme and parsed_login.netloc:
        origins.add(f"{parsed_login.scheme}://{parsed_login.netloc}")

    return sorted(origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=build_allowed_origins(),
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
    email = normalize_email(email)
    if not email:
        return None

    result = (
        supabase.table("usuarios")
        .select("*")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    data = result.data or []
    return data[0] if data else None


def get_auth_user_id_by_email(email: str) -> Optional[str]:
    email = normalize_email(email)
    if not email:
        return None

    page = 1
    per_page = 1000

    while True:
        auth_res = supabase.auth.admin.list_users(page=page, per_page=per_page)
        auth_users = getattr(auth_res, "users", []) or []

        for user in auth_users:
            user_email = normalize_email(getattr(user, "email", ""))
            if user_email == email:
                return str(getattr(user, "id", "")) or None

        if len(auth_users) < per_page:
            break

        page += 1

    return None


def safe_bool_access(subscription_status: str, requested_access_active: bool) -> bool:
    status = (subscription_status or "").strip().lower()
    if status in {"active", "trialing"}:
        return True
    if status in {"canceled", "cancelled", "unpaid", "incomplete_expired", "payment_failed", "inactive"}:
        return False
    return bool(requested_access_active)


def ensure_user_linked_to_auth(email: str, existing_user: Optional[dict] = None):
    email = normalize_email(email)
    row = existing_user or get_user_by_email(email)
    if not row:
        return None

    auth_user_id = row.get("auth_user_id")
    if auth_user_id:
        return row

    found_auth_id = get_auth_user_id_by_email(email)
    if not found_auth_id:
        return row

    update_payload = {
        "auth_user_id": found_auth_id,
        "updated_at": now_iso(),
    }

    current_id = row.get("id")
    if not current_id:
        update_payload["id"] = found_auth_id

    updated = (
        supabase.table("usuarios")
        .update(update_payload)
        .eq("email", email)
        .execute()
    )

    data = updated.data or []
    return data[0] if data else get_user_by_email(email)


def upsert_user_access(
    email: str,
    access_active: bool,
    subscription_status: str,
    plan: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    current_period_end: Optional[str] = None,
):
    email = normalize_email(email)
    if not email:
        raise ValueError("Email vacío en upsert_user_access")

    existing = get_user_by_email(email)
    auth_user_id = get_auth_user_id_by_email(email)

    is_admin = email == ADMIN_EMAIL
    effective_status = "active" if is_admin else (subscription_status or "inactive")
    effective_access = True if is_admin else safe_bool_access(effective_status, access_active)
    effective_plan = "pro" if is_admin else (plan or PLAN_NAME)
    effective_role = "admin" if is_admin else "user"

    base_payload = {
        "email": email,
        "role": effective_role,
        "plan": effective_plan,
        "access_active": effective_access,
        "subscription_status": effective_status,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "current_period_end": current_period_end,
        "updated_at": now_iso(),
    }

    if existing:
        # No pisamos full_name o phone aquí.
        if auth_user_id and not existing.get("auth_user_id"):
            base_payload["auth_user_id"] = auth_user_id

        result = (
            supabase.table("usuarios")
            .update(base_payload)
            .eq("email", email)
            .execute()
        )
        data = result.data or []
        return data[0] if data else get_user_by_email(email)

    insert_payload = {
        "id": auth_user_id or str(uuid4()),
        "auth_user_id": auth_user_id,
        "full_name": None,
        "phone": None,
        "created_at": now_iso(),
        **base_payload,
    }

    result = supabase.table("usuarios").insert(insert_payload).execute()
    data = result.data or []
    return data[0] if data else get_user_by_email(email)


def get_customer_email(customer_id: Optional[str]) -> Optional[str]:
    if not customer_id:
        return None
    customer = stripe.Customer.retrieve(customer_id)
    email = getattr(customer, "email", None)
    return normalize_email(email or "")


def get_subscription_status(subscription_id: Optional[str]) -> Optional[str]:
    if not subscription_id:
        return None
    subscription = stripe.Subscription.retrieve(subscription_id)
    status = getattr(subscription, "status", None)
    return (status or "").strip().lower() or None


def get_subscription_period_end(subscription_id: Optional[str]) -> Optional[str]:
    if not subscription_id:
        return None
    subscription = stripe.Subscription.retrieve(subscription_id)
    current_period_end = getattr(subscription, "current_period_end", None)
    return to_iso_from_unix(current_period_end)

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
    email = normalize_email(body.email)
    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")

    try:
        # Si ya existe en auth o en la tabla, lo dejamos preparado.
        existing = get_user_by_email(email)
        if existing:
            ensure_user_linked_to_auth(email, existing)

        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            customer_email=email,
            success_url=f"{LOGIN_URL.rstrip('/')}?checkout=success",
            cancel_url=f"{LOGIN_URL.rstrip('/')}?checkout=cancel",
            metadata={
                "email": email,
                "app_name": APP_NAME,
                "plan_name": PLAN_NAME,
            },
            subscription_data={
                "metadata": {
                    "email": email,
                    "app_name": APP_NAME,
                    "plan_name": PLAN_NAME,
                }
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
    email = normalize_email(body.email)
    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")

    user = get_user_by_email(email)

    # Si no existe y es el admin principal, lo creamos automáticamente.
    if not user and email == ADMIN_EMAIL:
        user = upsert_user_access(
            email=email,
            access_active=True,
            subscription_status="active",
            plan="pro",
        )

    if user:
        user = ensure_user_linked_to_auth(email, user)

    if not user:
        return {
            "ok": True,
            "exists": False,
            "access_active": False,
            "subscription_status": "inactive",
            "plan": "free",
            "should_show_paywall": True,
            "message": "Usuario no encontrado en tabla usuarios",
        }

    subscription_status = (user.get("subscription_status") or "inactive").strip().lower()
    access_active = bool(user.get("access_active", False))

    if email == ADMIN_EMAIL or user.get("role") == "admin":
        access_active = True
        subscription_status = "active"

    should_show_paywall = not access_active

    return {
        "ok": True,
        "exists": True,
        "access_active": access_active,
        "subscription_status": subscription_status,
        "plan": user.get("plan") or "free",
        "should_show_paywall": should_show_paywall,
        "message": "" if access_active else "Tu plan no está activo.",
    }


@app.post("/activate-user")
def activate_user(body: ActivateUserRequest):
    email = normalize_email(body.email)
    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")

    user = get_user_by_email(email)
    if not user:
        user = upsert_user_access(
            email=email,
            access_active=body.access_active,
            subscription_status=body.subscription_status or ("active" if body.access_active else "inactive"),
            plan=body.plan or PLAN_NAME,
            current_period_end=body.current_period_end,
        )
        return {"ok": True, "data": user}

    payload = {
        "access_active": body.access_active,
        "subscription_status": body.subscription_status or ("active" if body.access_active else "inactive"),
        "plan": body.plan or user.get("plan") or PLAN_NAME,
        "current_period_end": body.current_period_end,
        "updated_at": now_iso(),
    }

    result = (
        supabase.table("usuarios")
        .update(payload)
        .eq("email", email)
        .execute()
    )
    data = result.data or []
    return {"ok": True, "data": data[0] if data else get_user_by_email(email)}


@app.post("/webhook")
async def stripe_webhook(request: Request):
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
    data_object = event["data"]["object"].to_dict()

    try:
        if event_type == "checkout.session.completed":
            email = normalize_email(
                data_object.get("customer_details", {}).get("email")
                or data_object.get("customer_email")
                or data_object.get("metadata", {}).get("email")
            )
            subscription_id = data_object.get("subscription")
            customer_id = data_object.get("customer")
            status = get_subscription_status(subscription_id) or "active"
            current_period_end = get_subscription_period_end(subscription_id)

            if email:
                upsert_user_access(
                    email=email,
                    access_active=True,
                    subscription_status=status,
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_end=current_period_end,
                )

        elif event_type in {"customer.subscription.created", "customer.subscription.updated"}:
            customer_id = data_object.get("customer")
            subscription_id = data_object.get("id")
            status = (data_object.get("status") or "inactive").strip().lower()
            current_period_end = to_iso_from_unix(data_object.get("current_period_end"))
            email = (
                normalize_email(data_object.get("metadata", {}).get("email"))
                or get_customer_email(customer_id)
            )

            if email:
                upsert_user_access(
                    email=email,
                    access_active=(status in {"active", "trialing"}),
                    subscription_status=status,
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_end=current_period_end,
                )

        elif event_type == "customer.subscription.deleted":
            customer_id = data_object.get("customer")
            subscription_id = data_object.get("id")
            email = (
                normalize_email(data_object.get("metadata", {}).get("email"))
                or get_customer_email(customer_id)
            )

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

        elif event_type in {"invoice.paid", "invoice.payment_succeeded"}:
            customer_id = data_object.get("customer")
            subscription_id = data_object.get("subscription")
            email = (
                normalize_email(data_object.get("customer_email"))
                or normalize_email(data_object.get("metadata", {}).get("email"))
                or get_customer_email(customer_id)
            )
            current_period_end = get_subscription_period_end(subscription_id) if subscription_id else None
            status = get_subscription_status(subscription_id) if subscription_id else "active"

            if email:
                upsert_user_access(
                    email=email,
                    access_active=True,
                    subscription_status=status or "active",
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_end=current_period_end,
                )

        elif event_type == "invoice.payment_failed":
            customer_id = data_object.get("customer")
            subscription_id = data_object.get("subscription")
            email = (
                normalize_email(data_object.get("customer_email"))
                or normalize_email(data_object.get("metadata", {}).get("email"))
                or get_customer_email(customer_id)
            )

            if email:
                upsert_user_access(
                    email=email,
                    access_active=False,
                    subscription_status="payment_failed",
                    plan=PLAN_NAME,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_end=None,
                )

        return {"ok": True, "received": True, "event_type": event_type}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando webhook: {str(e)}")