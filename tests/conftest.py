"""pytest configuration and shared fixtures for ha-ideenergy tests."""
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# ── Make the workspace root importable ────────────────────────────────────────
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Stub out homeassistant and homeassistant_historical_sensor ────────────────
# These packages are only available inside a running HA environment.
# We create lightweight stubs so the integration modules can be imported in
# the test environment without a full HA installation.


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# homeassistant.core
_mock_dt_util = MagicMock()
_mock_dt_util.utc_from_timestamp = lambda ts: __import__("datetime").datetime.fromtimestamp(
    ts, tz=__import__("datetime").timezone.utc
)
_mock_dt_util.now = MagicMock(
    return_value=__import__("datetime").datetime(2026, 4, 19, 12, 0,
                                                   tzinfo=__import__("datetime").timezone.utc)
)

ha_core = _stub_module(
    "homeassistant.core",
    HomeAssistant=MagicMock,
    ServiceCall=MagicMock,
    callback=lambda fn: fn,
    dt_util=_mock_dt_util,
)

# homeassistant.config_entries
_stub_module("homeassistant.config_entries", ConfigEntry=MagicMock, ConfigEntryNotReady=Exception)

# homeassistant.const
_stub_module(
    "homeassistant.const",
    CONF_PASSWORD="password",
    CONF_USERNAME="username",
    UnitOfEnergy=MagicMock(KILO_WATT_HOUR="kWh", WATT_HOUR="Wh"),
)

# homeassistant.exceptions
_stub_module("homeassistant.exceptions", ServiceValidationError=Exception)

# homeassistant.helpers.*
helpers_pkg = _stub_module("homeassistant.helpers")
_stub_module("homeassistant.helpers.aiohttp_client", async_get_clientsession=MagicMock())
_stub_module("homeassistant.helpers.entity", DeviceInfo=dict)
_stub_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=MagicMock)
_stub_module("homeassistant.helpers.event", async_track_time_change=MagicMock())
_stub_module(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=object,
    CoordinatorEntity=object,
)
helpers_cv = MagicMock()
helpers_cv.date = lambda s: __import__("datetime").date.fromisoformat(s) if isinstance(s, str) else s
helpers_cv.boolean = lambda v: bool(v)
helpers_cv.string = lambda v: str(v)
helpers_pkg.config_validation = helpers_cv
helpers_pkg.entity_registry = MagicMock()
_stub_module("homeassistant.helpers", config_validation=helpers_cv, entity_registry=MagicMock())

# homeassistant.loader
_stub_module("homeassistant.loader", async_get_loaded_integration=AsyncMock())

# homeassistant.util
_stub_module(
    "homeassistant.util",
    slugify=lambda s, separator="-": s.lower().replace(" ", separator),
)

# homeassistant.components.*
recorder_pkg = _stub_module("homeassistant.components.recorder")
recorder_pkg.get_instance = MagicMock()

# Real StatisticData / StatisticMetaData stubs with proper field access
class _StatisticData:
    def __init__(self, *, start, state=None, sum=None, mean=None):
        self.start = start
        self.state = state
        self.sum = sum
        self.mean = mean

class _StatisticMetaData(dict):
    pass

_stub_module(
    "homeassistant.components.recorder.models",
    StatisticData=_StatisticData,
    StatisticMetaData=_StatisticMetaData,
)
_stub_module(
    "homeassistant.components.recorder.statistics",
    async_import_statistics=MagicMock(),
    async_add_external_statistics=MagicMock(),
    async_adjust_statistics=AsyncMock(),
    statistics_during_period=MagicMock(return_value={}),
)
_stub_module(
    "homeassistant.components.sensor",
    SensorEntity=object,
    SensorDeviceClass=MagicMock(),
    SensorStateClass=MagicMock(),
)
_stub_module("homeassistant.components", recorder=recorder_pkg)

# homeassistant (top-level)
_stub_module("homeassistant")

# homeassistant_historical_sensor
hs_stub = MagicMock()
hs_stub.HistoricalState = MagicMock
hs_stub.HistoricalSensor = object
hs_stub.hass_get_last_statistic = AsyncMock(return_value=None)
sys.modules["homeassistant_historical_sensor"] = hs_stub

# ideenergy (the upstream library)
_stub_module(
    "ideenergy",
    Client=MagicMock,
    MockClient=MagicMock,
    ClientError=Exception,
    PeriodValue=MagicMock,
    DemandAtInstant=MagicMock,
)

# voluptuous
vol_stub = _stub_module("voluptuous")
vol_stub.Schema = lambda x, **kw: x
vol_stub.Required = lambda k, **kw: k
vol_stub.Optional = lambda k, **kw: k

# ── Stub custom_components package WITHOUT running __init__.py ────────────────
# This allows importing backfill.py directly in tests while avoiding the
# Python 3.12+ `type X = ...` syntax in coordinator.py (which fails on 3.11).
import importlib.util as _ilu  # noqa: E402

def _load_module_from_path(dotted_name: str, path: str) -> types.ModuleType:
    """Load a module directly from a file path and register it in sys.modules."""
    spec = _ilu.spec_from_file_location(dotted_name, path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


_cc_pkg = types.ModuleType("custom_components")
_cc_ide_pkg = types.ModuleType("custom_components.ideenergy")
sys.modules["custom_components"] = _cc_pkg
sys.modules["custom_components.ideenergy"] = _cc_ide_pkg
_cc_pkg.ideenergy = _cc_ide_pkg

# Load const.py directly (no problematic imports)
_const_mod = _load_module_from_path(
    "custom_components.ideenergy.const",
    str(ROOT / "custom_components" / "ideenergy" / "const.py"),
)
_cc_ide_pkg.const = _const_mod

# Load backfill.py directly (standalone module, all its imports are already stubbed)
_backfill_mod = _load_module_from_path(
    "custom_components.ideenergy.backfill",
    str(ROOT / "custom_components" / "ideenergy" / "backfill.py"),
)
_cc_ide_pkg.backfill = _backfill_mod

# zoneinfo is part of stdlib (Python 3.9+), tzdata installed separately

