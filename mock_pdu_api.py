"""
Mock Schneider Electric SmartPDU Redfish-ish API (EC aggregation oriented)

Run:
  pip install fastapi uvicorn
  uvicorn mock_pdu_api:app --reload --port 8000

Test:
  pip install pytest
  pytest -q
"""

from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock SmartPDU Redfish API", version="0.2.0")


# -------------------------
# Static config / identity
# -------------------------

PDU_ID = "2"
PDU_MODEL = "Schneider Electric SmartPDU (Mock) 48-outlet"
OUTLET_COUNT = 48
BRANCH_COUNT = 3
MAINS_PHASES = 3

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "123456789"

SERVICE_UUID = "b2a6f2b7-5c4a-4ab3-a8df-51c6c5f3db66"

NOMINAL_VOLTAGE = 230.0
NOMINAL_FREQ = 50.0

START_EPOCH = time.time()

# Outlet connection/load model (W). Outlets not listed => not connected => ~0W
CONNECTED_OUTLET_LOAD_W: Dict[int, float] = {
    1: 140.0,
    2: 45.0,
    3: 90.0,
    10: 220.0,
    12: 75.0,
    20: 180.0,
    44: 260.0,  # referenced in your doc snippet
}

# In-memory outlet state
OUTLET_STATE: Dict[int, str] = {i: "On" for i in range(1, OUTLET_COUNT + 1)}


# -------------------------
# In-memory state
# -------------------------

@dataclass
class Session:
    session_id: str
    username: str
    token: str
    created_epoch: float


@dataclass
class Subscription:
    sub_id: str
    destination: str
    event: str
    context: str
    protocol: str
    created_epoch: float


USERS: Dict[str, Dict[str, Any]] = {
    DEFAULT_ADMIN_USER: {
        "username": DEFAULT_ADMIN_USER,
        "password": DEFAULT_ADMIN_PASS,
        "role": "Administrator",
        "enabled": True,
    }
}

SESSIONS: Dict[str, Session] = {}
TOKENS_TO_SESSION: Dict[str, str] = {}
SUBSCRIPTIONS: Dict[str, Subscription] = {}


# -------------------------
# Redfish-ish helpers
# -------------------------

def rf_status(state: str = "Enabled", health: str = "OK") -> dict:
    return {"State": state, "Health": health}


def rf_resource(
    *,
    odata_id: str,
    odata_type: str,
    rid: str,
    name: str,
    status: Optional[dict] = None,
    **fields: Any,
) -> dict:
    return {
        "@odata.id": odata_id,
        "@odata.type": odata_type,
        "Id": rid,
        "Name": name,
        "Status": status or rf_status(),
        **fields,
    }


def rf_collection(
    *,
    odata_id: str,
    odata_type: str,
    name: str,
    member_uris: list[str],
) -> dict:
    return {
        "@odata.id": odata_id,
        "@odata.type": odata_type,
        "Name": name,
        "Members@odata.count": len(member_uris),
        "Members": [{"@odata.id": u} for u in member_uris],
    }


def rf_sensor(
    *,
    odata_id: str,
    rid: str,
    name: str,
    reading: Optional[float],
    units: str,
    reading_type: str,
    context: str,
    status: Optional[dict] = None,
    **fields: Any,
) -> dict:
    payload = {
        "@odata.id": odata_id,
        "@odata.type": "#Sensor.v1_7_0.Sensor",
        "Id": rid,
        "Name": name,
        "Status": status or rf_status(),
        "ReadingType": reading_type,
        "PhysicalContext": context,
        "ReadingUnits": units,
        "Reading": None if reading is None else round(float(reading), 4),
        **fields,
    }
    return payload


def rf_error_payload(code: str, message: str, extended: Optional[list[dict]] = None) -> dict:
    err = {"code": code, "message": message}
    if extended:
        err["@Message.ExtendedInfo"] = extended
    return {"error": err}


def raise_rf(status: int, code: str, message: str) -> None:
    # We keep detail as dict, and our exception handler below will return it as JSON.
    raise HTTPException(status_code=status, detail=rf_error_payload(code, message))


# -------------------------
# Auth helpers
# -------------------------

def _parse_basic_auth(authorization: Optional[str]) -> Tuple[str, str]:
    if not authorization or not authorization.startswith("Basic "):
        raise_rf(401, "Base.1.0.InsufficientPrivilege", "Missing or invalid Authorization header (Basic required)")
    b64 = authorization.split(" ", 1)[1].strip()
    try:
        raw = base64.b64decode(b64).decode("utf-8")
    except Exception:
        raise_rf(401, "Base.1.0.InsufficientPrivilege", "Invalid Basic auth encoding")
    if ":" not in raw:
        raise_rf(401, "Base.1.0.InsufficientPrivilege", "Invalid Basic auth format")
    username, password = raw.split(":", 1)
    return username, password


def require_basic_auth(request: Request) -> str:
    authorization = request.headers.get("Authorization")
    username, password = _parse_basic_auth(authorization)
    user = USERS.get(username)
    if not user or user["password"] != password or not user.get("enabled", False):
        raise_rf(401, "Base.1.0.InvalidAuthenticationToken", "Invalid credentials")
    return username


def require_token(x_authtoken: Optional[str]) -> Session:
    if not x_authtoken:
        raise_rf(401, "Base.1.0.InvalidAuthenticationToken", "Missing X-Auth-Token")
    session_id = TOKENS_TO_SESSION.get(x_authtoken)
    if not session_id or session_id not in SESSIONS:
        raise_rf(401, "Base.1.0.InvalidAuthenticationToken", "Invalid X-Auth-Token")
    return SESSIONS[session_id]


# -------------------------
# Measurement model 
# -------------------------

def _small_jitter(seed: int) -> float:
    # deterministic jitter in [-0.03, +0.03]
    v = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    return ((v % 6001) / 6000.0) * 0.06 - 0.03


def outlet_connected(outlet: int) -> bool:
    return outlet in CONNECTED_OUTLET_LOAD_W


def outlet_load_w(outlet: int) -> float:
    if OUTLET_STATE.get(outlet, "On") != "On":
        return 0.0
    base = CONNECTED_OUTLET_LOAD_W.get(outlet, 0.0)
    if base <= 0:
        return 0.0
    seconds = int(time.time() - START_EPOCH)
    jitter = _small_jitter(seed=outlet * 100000 + seconds // 5)
    return max(0.0, base * (1.0 + jitter))


def outlet_voltage_v(outlet: int) -> float:
    seconds = int(time.time() - START_EPOCH)
    jitter = _small_jitter(seed=outlet * 999 + seconds // 10)
    return NOMINAL_VOLTAGE * (1.0 + jitter * 0.15)


def outlet_current_a(outlet: int) -> float:
    v = outlet_voltage_v(outlet)
    p = outlet_load_w(outlet)
    if v <= 0.0:
        return 0.0
    return p / v


def outlet_energy_kwh(outlet: int) -> float:
    # monotonic accumulation based on configured base load for stable tests.
    hours = (time.time() - START_EPOCH) / 3600.0
    base = CONNECTED_OUTLET_LOAD_W.get(outlet, 0.0)
    if OUTLET_STATE.get(outlet, "On") != "On":
        base = 0.0
    return max(0.0, (base * hours) / 1000.0)


def pdu_total_power_w() -> float:
    return sum(outlet_load_w(i) for i in range(1, OUTLET_COUNT + 1))


def pdu_total_energy_kwh() -> float:
    return sum(outlet_energy_kwh(i) for i in range(1, OUTLET_COUNT + 1))


def mains_voltage_v(phase: int) -> float:
    seconds = int(time.time() - START_EPOCH)
    jitter = _small_jitter(seed=phase * 123456 + seconds // 10)
    return NOMINAL_VOLTAGE * (1.0 + jitter * 0.10)


def mains_current_a(phase: int) -> float:
    p = pdu_total_power_w()
    v = mains_voltage_v(phase)
    if v <= 0:
        return 0.0
    return (p / MAINS_PHASES) / v


def freq_hz() -> float:
    seconds = int(time.time() - START_EPOCH)
    jitter = _small_jitter(seed=424242 + seconds // 30)
    return NOMINAL_FREQ * (1.0 + jitter * 0.01)


# -------------------------
# Error handling
# -------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    # If detail is already a Redfish-like dict, return it. Otherwise wrap it.
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=rf_error_payload("Base.1.0.GeneralError", str(exc.detail)),
    )


# -------------------------
# GET endpoints (Basic Auth)
# -------------------------

@app.get("/redfish/v1/")
def get_root(request: Request):
    require_basic_auth(request)
    # ServiceRoot generally doesn’t include Status
    return {
        "@odata.id": "/redfish/v1/",
        "@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
        "Id": "RootService",
        "Name": "Root Service",
        "RedfishVersion": "1.10.0",
        "UUID": SERVICE_UUID,
        "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
        "AccountService": {"@odata.id": "/redfish/v1/AccountService"},
        "Managers": {"@odata.id": "/redfish/v1/Managers"},
        "PowerEquipment": {"@odata.id": "/redfish/v1/PowerEquipment"},
        "EventService": {"@odata.id": "/redfish/v1/EventService"},
    }


# ---- SessionService

@app.get("/redfish/v1/SessionService")
def get_session_service(request: Request):
    require_basic_auth(request)
    return rf_resource(
        odata_id="/redfish/v1/SessionService",
        odata_type="#SessionService.v1_1_0.SessionService",
        rid="SessionService",
        name="Session Service",
        Sessions={"@odata.id": "/redfish/v1/SessionService/Sessions"},
    )


@app.get("/redfish/v1/SessionService/Sessions")
def get_sessions(request: Request):
    require_basic_auth(request)
    members = [f"/redfish/v1/SessionService/Sessions/{sid}" for sid in sorted(SESSIONS.keys())]
    return rf_collection(
        odata_id="/redfish/v1/SessionService/Sessions",
        odata_type="#SessionCollection.SessionCollection",
        name="Session Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/SessionService/Sessions/{session_id}")
def get_session(request: Request, session_id: str):
    require_basic_auth(request)
    s = SESSIONS.get(session_id)
    if not s:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Session not found")
    return rf_resource(
        odata_id=f"/redfish/v1/SessionService/Sessions/{session_id}",
        odata_type="#Session.v1_1_0.Session",
        rid=s.session_id,
        name="Session",
        status=rf_status(),
        UserName=s.username,
        Created=s.created_epoch,
    )


# ---- AccountService

@app.get("/redfish/v1/AccountService")
def get_account_service(request: Request):
    require_basic_auth(request)
    return rf_resource(
        odata_id="/redfish/v1/AccountService",
        odata_type="#AccountService.v1_5_0.AccountService",
        rid="AccountService",
        name="Account Service",
        Accounts={"@odata.id": "/redfish/v1/AccountService/Accounts"},
        Roles={"@odata.id": "/redfish/v1/AccountService/Roles"},
    )


@app.get("/redfish/v1/AccountService/Accounts")
def get_accounts(request: Request):
    require_basic_auth(request)
    members = [f"/redfish/v1/AccountService/Accounts/{u}" for u in sorted(USERS.keys())]
    return rf_collection(
        odata_id="/redfish/v1/AccountService/Accounts",
        odata_type="#ManagerAccountCollection.ManagerAccountCollection",
        name="Account Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/AccountService/Accounts/{username}")
def get_account(request: Request, username: str):
    require_basic_auth(request)
    u = USERS.get(username)
    if not u:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "User not found")
    return rf_resource(
        odata_id=f"/redfish/v1/AccountService/Accounts/{username}",
        odata_type="#ManagerAccount.v1_9_0.ManagerAccount",
        rid=username,
        name=f"Account {username}",
        UserName=u["username"],
        RoleId=u["role"],
        Enabled=bool(u["enabled"]),
    )


@app.get("/redfish/v1/AccountService/Roles")
def get_roles(request: Request):
    require_basic_auth(request)
    roles = ["Administrator", "Operator", "ReadOnly"]
    members = [f"/redfish/v1/AccountService/Roles/{r}" for r in roles]
    return rf_collection(
        odata_id="/redfish/v1/AccountService/Roles",
        odata_type="#RoleCollection.RoleCollection",
        name="Role Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/AccountService/Roles/{rolename}")
def get_role(request: Request, rolename: str):
    require_basic_auth(request)
    if rolename not in {"Administrator", "Operator", "ReadOnly"}:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Role not found")
    return rf_resource(
        odata_id=f"/redfish/v1/AccountService/Roles/{rolename}",
        odata_type="#Role.v1_3_0.Role",
        rid=rolename,
        name=rolename,
    )


# ---- Managers

@app.get("/redfish/v1/Managers")
def get_managers(request: Request):
    require_basic_auth(request)
    members = ["/redfish/v1/Managers/manager"]
    return rf_collection(
        odata_id="/redfish/v1/Managers",
        odata_type="#ManagerCollection.ManagerCollection",
        name="Manager Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/Managers/manager")
def get_manager(request: Request):
    require_basic_auth(request)
    return rf_resource(
        odata_id="/redfish/v1/Managers/manager",
        odata_type="#Manager.v1_11_0.Manager",
        rid="manager",
        name="Mock PDU Manager",
        NetworkProtocol={"@odata.id": "/redfish/v1/Managers/managers/NetworkProtocol"},
        LogServices={"@odata.id": "/redfish/v1/Managers/1/LogServices"},
    )


@app.get("/redfish/v1/Managers/managers/NetworkProtocol")
def get_network_protocol(request: Request):
    require_basic_auth(request)
    return rf_resource(
        odata_id="/redfish/v1/Managers/managers/NetworkProtocol",
        odata_type="#ManagerNetworkProtocol.v1_6_0.ManagerNetworkProtocol",
        rid="NetworkProtocol",
        name="Network Protocol",
        HTTP={"Port": 80},
        HTTPS={"Port": 443},
        SSDP={"ProtocolEnabled": False},
    )


@app.get("/redfish/v1/Managers/1/LogServices")
def get_log_services(request: Request):
    require_basic_auth(request)
    members = ["/redfish/v1/Managers/1/LogServices/Log"]
    return rf_collection(
        odata_id="/redfish/v1/Managers/1/LogServices",
        odata_type="#LogServiceCollection.LogServiceCollection",
        name="Log Service Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/Managers/1/LogServices/Log")
def get_log(request: Request):
    require_basic_auth(request)
    return rf_resource(
        odata_id="/redfish/v1/Managers/1/LogServices/Log",
        odata_type="#LogService.v1_2_0.LogService",
        rid="Log",
        name="System Log",
        Entries={"@odata.id": "/redfish/v1/Managers/1/LogServices/Log/Entries"},
    )


@app.get("/redfish/v1/Managers/1/LogServices/Log/Entries")
def get_log_entries(request: Request):
    require_basic_auth(request)
    # A lightweight Entries “collection-like” payload
    entries = [
        {
            "@odata.id": "/redfish/v1/Managers/1/LogServices/Log/Entries/1",
            "@odata.type": "#LogEntry.v1_9_0.LogEntry",
            "Id": "1",
            "Name": "Log Entry 1",
            "Message": "Mock PDU boot",
            "Created": START_EPOCH,
            "Severity": "OK",
        },
        {
            "@odata.id": "/redfish/v1/Managers/1/LogServices/Log/Entries/2",
            "@odata.type": "#LogEntry.v1_9_0.LogEntry",
            "Id": "2",
            "Name": "Log Entry 2",
            "Message": "REST API enabled",
            "Created": START_EPOCH + 1,
            "Severity": "OK",
        },
    ]
    return {
        "@odata.id": "/redfish/v1/Managers/1/LogServices/Log/Entries",
        "@odata.type": "#LogEntryCollection.LogEntryCollection",
        "Name": "Log Entry Collection",
        "Members@odata.count": len(entries),
        "Members": entries,
    }


# ---- PowerEquipment

@app.get("/redfish/v1/PowerEquipment")
def get_power_equipment(request: Request):
    require_basic_auth(request)
    return rf_resource(
        odata_id="/redfish/v1/PowerEquipment",
        odata_type="#PowerEquipment.v1_0_0.PowerEquipment",
        rid="PowerEquipment",
        name="Power Equipment",
        RackPDUs={"@odata.id": "/redfish/v1/PowerEquipment/RackPDUs"},
    )


@app.get("/redfish/v1/PowerEquipment/RackPDUs")
def get_rack_pdus(request: Request):
    require_basic_auth(request)
    members = [f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}"]
    return rf_collection(
        odata_id="/redfish/v1/PowerEquipment/RackPDUs",
        odata_type="#PowerDistributionCollection.PowerDistributionCollection",
        name="Rack PDU Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}")
def get_rack_pdu(request: Request, pdu_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    return rf_resource(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}",
        odata_type="#PowerDistribution.v1_1_0.PowerDistribution",
        rid=PDU_ID,
        name=f"Rack PDU {PDU_ID}",
        Model=PDU_MODEL,
        SerialNumber=f"SE-MOCK-{PDU_ID.zfill(4)}",
        Manufacturer="Schneider Electric",
        Outlets={"@odata.id": f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Outlets"},
        Branches={"@odata.id": f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Branches"},
        Mains={"@odata.id": f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Mains"},
        Metrics={"@odata.id": f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Metrics"},
        Sensors={"@odata.id": f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Sensors"},
    )


# ---- Metrics

@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Metrics")
def get_metrics(request: Request, pdu_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    return rf_resource(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Metrics",
        odata_type="#PowerMetrics.v1_0_0.PowerMetrics",
        rid=f"Metrics-{PDU_ID}",
        name="PDU Metrics",
        # EC aggregation friendly fields:
        PowerWatts=round(pdu_total_power_w(), 2),
        EnergykWh=round(pdu_total_energy_kwh(), 4),
        FrequencyHz=round(freq_hz(), 2),
    )


# ---- Branches

@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Branches")
def get_branches(request: Request, pdu_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")
    members = [f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Branches/{i}" for i in range(1, BRANCH_COUNT + 1)]
    return rf_collection(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Branches",
        odata_type="#CircuitCollection.CircuitCollection",
        name="Branch Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Branches/{cbnumber}")
def get_branch(request: Request, pdu_id: str, cbnumber: int):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")
    if cbnumber < 1 or cbnumber > BRANCH_COUNT:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Branch not found")

    return rf_resource(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Branches/{cbnumber}",
        odata_type="#Circuit.v1_0_0.Circuit",
        rid=str(cbnumber),
        name=f"Branch {cbnumber}",
    )


# ---- Outlets

@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Outlets")
def get_outlets(request: Request, pdu_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    members = [f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Outlets/{i}" for i in range(1, OUTLET_COUNT + 1)]
    return rf_collection(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Outlets",
        odata_type="#OutletCollection.OutletCollection",
        name="Outlet Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Outlets/{outletnumber}")
def get_outlet(request: Request, pdu_id: str, outletnumber: int):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")
    if outletnumber < 1 or outletnumber > OUTLET_COUNT:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Outlet not found")

    state = OUTLET_STATE[outletnumber]
    connected = outlet_connected(outletnumber)
    rated = CONNECTED_OUTLET_LOAD_W.get(outletnumber, 0.0)

    return rf_resource(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Outlets/{outletnumber}",
        odata_type="#Outlet.v1_0_0.Outlet",
        rid=str(outletnumber),
        name=f"Outlet {outletnumber}",
        # Redfish-ish status (Enabled if On, Disabled if Off)
        status=rf_status(state="Enabled" if state == "On" else "Disabled", health="OK"),
        Connected=connected,
        RatedLoadWatts=rated,
        # Actions advertised (even if you don't implement this action endpoint yet)
        Actions={
            "#Outlet.PowerControl": {
                "target": f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Outlets/{outletnumber}/Actions/Outlet.PowerControl",
                "PowerState@Redfish.AllowableValues": ["On", "Off", "Cycle"],
            }
        },
    )


# ---- Mains

@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Mains")
def get_mains(request: Request, pdu_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    members = [f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Mains/AC1"]
    return rf_collection(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Mains",
        odata_type="#PowerSupplyCollection.PowerSupplyCollection",
        name="Mains Collection",
        member_uris=members,
    )


@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Mains/AC1")
def get_mains_ac1(request: Request, pdu_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    return rf_resource(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Mains/AC1",
        odata_type="#PowerSupply.v1_5_0.PowerSupply",
        rid="AC1",
        name="Main AC Input",
        Phases=MAINS_PHASES,
    )


# ---- Sensors index + Sensor GET (pattern matching the SmartPDU URL style)

@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Sensors")
def get_sensors_root(request: Request, pdu_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    # Redfish would often provide a collection. Your device lists sensor endpoints by convention,
    # so we keep it a resource and rely on the specific sensor URIs.
    return rf_resource(
        odata_id=f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Sensors",
        odata_type="#SensorCollection.SensorCollection",
        rid=f"Sensors-{PDU_ID}",
        name="Sensors",
        Note="Access individual sensors via /Sensors/<SensorId> (e.g., PowerOUTLET44, FreqMains, PDUPower).",
    )


@app.get("/redfish/v1/PowerEquipment/RackPDUs/{pdu_id}/Sensors/{sensor_id}")
def get_sensor(request: Request, pdu_id: str, sensor_id: str):
    require_basic_auth(request)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    base_uri = f"/redfish/v1/PowerEquipment/RackPDUs/{PDU_ID}/Sensors/{sensor_id}"

    # Outlet sensors: CurrentOUTLET#, VoltageOUTLET#, PowerOUTLET#, EnergyOUTLET#
    for prefix, rtype, units in (
        ("CurrentOUTLET", "Current", "A"),
        ("VoltageOUTLET", "Voltage", "V"),
        ("PowerOUTLET", "Power", "W"),
        ("EnergyOUTLET", "Energy", "kWh"),
    ):
        if sensor_id.startswith(prefix):
            n_str = sensor_id[len(prefix):]
            if not n_str.isdigit():
                raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Invalid outlet sensor format")
            outlet = int(n_str)
            if outlet < 1 or outlet > OUTLET_COUNT:
                raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Outlet not found")

            if prefix == "CurrentOUTLET":
                reading = outlet_current_a(outlet)
            elif prefix == "VoltageOUTLET":
                reading = outlet_voltage_v(outlet)
            elif prefix == "PowerOUTLET":
                reading = outlet_load_w(outlet)
            else:
                reading = outlet_energy_kwh(outlet)

            health = "OK" if (outlet_connected(outlet) or prefix in ("VoltageOUTLET",)) else "OK"
            status = rf_status(state="Enabled", health=health)

            return rf_sensor(
                odata_id=base_uri,
                rid=sensor_id,
                name=f"Outlet {outlet} {rtype}",
                reading=reading,
                units=units,
                reading_type=rtype,
                context="Outlet",
                status=status,
            )

    # Mains sensors
    if sensor_id == "FreqMains":
        return rf_sensor(
            odata_id=base_uri,
            rid=sensor_id,
            name="Mains Frequency",
            reading=freq_hz(),
            units="Hz",
            reading_type="Frequency",
            context="ACInput",
        )

    if sensor_id == "PDUPower":
        return rf_sensor(
            odata_id=base_uri,
            rid=sensor_id,
            name="PDU Total Power",
            reading=pdu_total_power_w(),
            units="W",
            reading_type="Power",
            context="PowerSubsystem",
        )

    if sensor_id.startswith("CurrentMains"):
        phase_str = sensor_id[len("CurrentMains"):]
        if not phase_str.isdigit():
            raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Invalid mains current sensor")
        phase = int(phase_str)
        if phase < 1 or phase > MAINS_PHASES:
            raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Mains phase not found")

        return rf_sensor(
            odata_id=base_uri,
            rid=sensor_id,
            name=f"Mains Phase {phase} Current",
            reading=mains_current_a(phase),
            units="A",
            reading_type="Current",
            context="ACInput",
        )

    if sensor_id.startswith("VoltageMains"):
        idx_str = sensor_id[len("VoltageMains"):]
        if not idx_str.isdigit():
            raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Invalid mains voltage sensor")
        idx = int(idx_str)
        if idx < 1 or idx > 6:
            raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Mains voltage index not found")
        phase = ((idx - 1) % MAINS_PHASES) + 1

        return rf_sensor(
            odata_id=base_uri,
            rid=sensor_id,
            name=f"Mains Voltage Channel {idx} (Phase {phase})",
            reading=mains_voltage_v(phase),
            units="V",
            reading_type="Voltage",
            context="ACInput",
        )

    if sensor_id.startswith("PowerMains"):
        idx_str = sensor_id[len("PowerMains"):]
        if not idx_str.isdigit():
            raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Invalid mains power sensor")
        idx = int(idx_str)
        if idx < 1 or idx > 6:
            raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Mains power index not found")

        return rf_sensor(
            odata_id=base_uri,
            rid=sensor_id,
            name=f"Mains Power Channel {idx}",
            reading=pdu_total_power_w() / 6.0,
            units="W",
            reading_type="Power",
            context="ACInput",
        )

    raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Unknown sensor")


# ---- EventService

@app.get("/redfish/v1/EventService")
def get_event_service(request: Request):
    require_basic_auth(request)
    return rf_resource(
        odata_id="/redfish/v1/EventService",
        odata_type="#EventService.v1_6_0.EventService",
        rid="EventService",
        name="Event Service",
        Subscriptions={"@odata.id": "/redfish/v1/EventService/Subscriptions"},
    )


@app.get("/redfish/v1/EventService/Subscriptions/{sub_id}")
def get_subscription(request: Request, sub_id: str):
    require_basic_auth(request)
    s = SUBSCRIPTIONS.get(sub_id)
    if not s:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Subscription not found")

    return rf_resource(
        odata_id=f"/redfish/v1/EventService/Subscriptions/{sub_id}",
        odata_type="#EventDestination.v1_8_0.EventDestination",
        rid=sub_id,
        name=f"Subscription {sub_id}",
        Destination=s.destination,
        EventTypes=[s.event],
        Context=s.context,
        Protocol=s.protocol,
        Created=s.created_epoch,
    )


# -------------------------
# POST endpoints
# -------------------------

@app.post("/redfish/v1/SessionService/Sessions")
async def create_session(request: Request, response: Response):
    # Accept body credentials exactly as your snippet
    body = await request.json()
    username = body.get("username")
    password = body.get("password")
    if not username or not password:
        raise_rf(400, "Base.1.0.PropertyMissing", "username/password required")

    user = USERS.get(username)
    if not user or user["password"] != password:
        raise_rf(401, "Base.1.0.InvalidAuthenticationToken", "Invalid credentials")

    session_id = secrets.token_hex(8)
    token = secrets.token_hex(16)

    s = Session(session_id=session_id, username=username, token=token, created_epoch=time.time())
    SESSIONS[session_id] = s
    TOKENS_TO_SESSION[token] = session_id

    response.headers["X-Auth-Token"] = token
    response.headers["Location"] = f"/redfish/v1/SessionService/Sessions/{session_id}"
    response.status_code = 201

    return rf_resource(
        odata_id=f"/redfish/v1/SessionService/Sessions/{session_id}",
        odata_type="#Session.v1_1_0.Session",
        rid=session_id,
        name="Session",
        UserName=username,
        Created=s.created_epoch,
        # Some implementations echo token in body (your original did); keep it for convenience
        **{"X-Auth-Token": token},
    )


@app.post("/redfish/v1/AccountService/Accounts")
async def create_account(request: Request, response: Response):
    require_basic_auth(request)
    body = await request.json()

    username = body.get("UserName") or body.get("username")
    password = body.get("Password") or body.get("password")
    role = body.get("RoleId") or body.get("role") or "Operator"

    if not username or not password:
        raise_rf(400, "Base.1.0.PropertyMissing", "UserName/Password required")
    if username in USERS:
        raise_rf(409, "Base.1.0.ResourceAlreadyExists", "User already exists")

    USERS[username] = {"username": username, "password": password, "role": role, "enabled": True}
    response.status_code = 201
    response.headers["Location"] = f"/redfish/v1/AccountService/Accounts/{username}"

    return rf_resource(
        odata_id=f"/redfish/v1/AccountService/Accounts/{username}",
        odata_type="#ManagerAccount.v1_9_0.ManagerAccount",
        rid=username,
        name=f"Account {username}",
        UserName=username,
        RoleId=role,
        Enabled=True,
    )


@app.post("/redfish/v1/EventService/Subscriptions")
async def create_subscription(
    request: Request,
    response: Response,
    x_authtoken: Optional[str] = Header(default=None, alias="X-Auth-Token"),
):
    require_token(x_authtoken)
    body = await request.json()

    destination = body.get("destination")
    event = body.get("event", "Alert")
    context = body.get("context", "")
    protocol = body.get("protocol", "redfish")

    if not destination:
        raise_rf(400, "Base.1.0.PropertyMissing", "destination required")

    sub_id = str(len(SUBSCRIPTIONS) + 1)
    SUBSCRIPTIONS[sub_id] = Subscription(
        sub_id=sub_id,
        destination=destination,
        event=event,
        context=context,
        protocol=protocol,
        created_epoch=time.time(),
    )

    response.status_code = 201
    response.headers["Location"] = f"/redfish/v1/EventService/Subscriptions/{sub_id}"

    return rf_resource(
        odata_id=f"/redfish/v1/EventService/Subscriptions/{sub_id}",
        odata_type="#EventDestination.v1_8_0.EventDestination",
        rid=sub_id,
        name=f"Subscription {sub_id}",
        Destination=destination,
        EventTypes=[event],
        Context=context,
        Protocol=protocol,
        Created=SUBSCRIPTIONS[sub_id].created_epoch,
    )


@app.post("/redfish/v1/PowerDistribution/{pdu_id}/PowerControl/Loadsegment/{loadseg_id}/")
async def power_control_loadsegment(
    pdu_id: str,
    loadseg_id: str,
    request: Request,
    x_authtoken: Optional[str] = Header(default=None, alias="X-Auth-Token"),
):
    require_token(x_authtoken)
    if pdu_id != PDU_ID:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "PDU not found")

    body = await request.json()
    action = (body.get("Action") or body.get("action") or "").strip().lower()
    if action not in {"on", "off", "cycle"}:
        raise_rf(400, "Base.1.0.PropertyValueNotInList", "Action must be one of: On, Off, Cycle")

    try:
        seg = int(loadseg_id)
    except ValueError:
        raise_rf(400, "Base.1.0.PropertyValueFormatError", "Invalid loadseg_id")

    if seg not in {1, 2, 3}:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Load segment not found")

    # Simple mapping: segments are 16-outlet blocks: 1..16, 17..32, 33..48
    start = (seg - 1) * 16 + 1
    end = seg * 16

    if action == "cycle":
        for i in range(start, end + 1):
            OUTLET_STATE[i] = "Off"
        for i in range(start, end + 1):
            OUTLET_STATE[i] = "On"
    else:
        new_state = "On" if action == "on" else "Off"
        for i in range(start, end + 1):
            OUTLET_STATE[i] = new_state

    return rf_resource(
        odata_id=f"/redfish/v1/PowerDistribution/{PDU_ID}/PowerControl/Loadsegment/{seg}/",
        odata_type="#ActionResponse.v1_0_0.ActionResponse",
        rid=f"Loadsegment-{seg}",
        name="Loadsegment PowerControl Result",
        PduId=pdu_id,
        LoadSegment=seg,
        ActionApplied=action,
        OutletsAffected=[start, end],
    )


# -------------------------
# DELETE endpoints (Basic Auth)
# -------------------------

@app.delete("/redfish/v1/SessionService/Sessions/{session_id}")
def delete_session(request: Request, session_id: str):
    require_basic_auth(request)
    s = SESSIONS.pop(session_id, None)
    if not s:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Session not found")
    TOKENS_TO_SESSION.pop(s.token, None)
    # Redfish commonly returns 204 No Content
    return Response(status_code=204)


@app.delete("/redfish/v1/AccountService/Accounts/{username}")
def delete_account(request: Request, username: str):
    require_basic_auth(request)
    if username == DEFAULT_ADMIN_USER:
        raise_rf(403, "Base.1.0.InsufficientPrivilege", "Cannot delete admin user")
    if username not in USERS:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "User not found")
    USERS.pop(username, None)
    return Response(status_code=204)


@app.delete("/redfish/v1/EventService/Subscriptions/{sub_id}")
def delete_subscription(request: Request, sub_id: str):
    require_basic_auth(request)
    if sub_id not in SUBSCRIPTIONS:
        raise_rf(404, "Base.1.0.ResourceMissingAtURI", "Subscription not found")
    SUBSCRIPTIONS.pop(sub_id, None)
    return Response(status_code=204)

