"""Saved Wi-Fi passwords: wifi_vault.json, netsh password read, forget, and connect-new.

Every netsh call is mocked, so no profile is read, added, deleted or joined. The runtime dir is a temp dir.
"""
import json
import os
import xml.etree.ElementTree as ET

import pytest

from src.agent import tools as tools_mod
from src.services import network_service
from src.services.network_service import NetworkError

NS = "{http://www.microsoft.com/networking/WLAN/profile/v1}"

PROFILE_WITH_KEY = """
Profile Wi-Fi-HomeNet
Security settings
    Authentication         : WPA2-Personal
    Cipher                 : CCMP
    Security key           : Present
    Key Content            : c0lon:in:pass
"""

PROFILE_OPEN = """
Profile Cafe 5G
Security settings
    Authentication         : Open
    Security key           : Absent
"""


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path / "data"))
    yield


def _vault_file():
    return network_service.vault_path()


class FakeNetsh:
    """Records netsh argv; answers profile reads from a dict; reads temp XML when it is added."""

    def __init__(self, profiles=None, fail_show=False):
        self.profiles = profiles or {}
        self.fail_show = fail_show
        self.calls = []
        self.added_xml = []   # contents of each temp XML file, read while netsh would read it
        self.added_paths = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:4] == ["netsh", "wlan", "show", "profile"]:
            name = argv[4].split('name="', 1)[1].rstrip('"')
            if self.fail_show or name not in self.profiles:
                raise NetworkError(f'The profile "{name}" is not found on the interface.')
            return self.profiles[name]
        if argv[:4] == ["netsh", "wlan", "add", "profile"]:
            path = argv[4].split('filename="', 1)[1].rstrip('"')
            self.added_paths.append(path)
            with open(path, encoding="utf-8") as fh:
                self.added_xml.append(fh.read())
            return ""
        return ""

    def verbs(self):
        return [" ".join(c[1:4]) for c in self.calls]


# --- XML profile escaping -----------------------------------------------------

@pytest.mark.parametrize("ssid,password", [
    ("Cafe & <Bar> \"5G\"", "pa\"ss<w>&'ord1"),
    ("Home's Net", "p@ss word 1"),
    ("<name>evil</name>", "</keyMaterial><x>y"),
])
def test_profile_xml_is_well_formed_and_escaped(ssid, password):
    xml = network_service.build_wpa2_profile_xml(ssid, password)
    root = ET.fromstring(xml)   # raises if the XML is broken or injected
    assert root.find(f"{NS}name").text == ssid
    assert root.find(f"{NS}SSIDConfig/{NS}SSID/{NS}name").text == ssid
    assert root.find(f"{NS}MSM/{NS}security/{NS}sharedKey/{NS}keyMaterial").text == password
    assert root.find(f"{NS}MSM/{NS}security/{NS}authEncryption/{NS}authentication").text == "WPA2PSK"
    assert root.find(f"{NS}MSM/{NS}security/{NS}authEncryption/{NS}encryption").text == "AES"


def test_profile_xml_rejects_bad_inputs():
    with pytest.raises(NetworkError):
        network_service.build_wpa2_profile_xml("Net", "short")
    with pytest.raises(NetworkError):
        network_service.build_wpa2_profile_xml("Net", "x" * 64)
    with pytest.raises(NetworkError):
        network_service.build_wpa2_profile_xml("bad\x01ssid", "password1")


def test_password_length_limits():
    assert network_service.validate_password("a" * 8) == "a" * 8
    assert network_service.validate_password("a" * 63) == "a" * 63
    with pytest.raises(NetworkError):
        network_service.validate_password("a" * 7)
    with pytest.raises(NetworkError):
        network_service.validate_password("a" * 64)


# --- wifi_vault.json persistence ----------------------------------------------

def test_vault_persists_across_instances_and_is_in_runtime_dir():
    network_service.vault_put("HomeNet", "secret-one")
    network_service.vault_put("Cafe 5G", "secret-two")

    assert _vault_file().parent == network_service.vault_path().parent
    assert _vault_file().name == "wifi_vault.json"
    on_disk = json.loads(_vault_file().read_text(encoding="utf-8"))
    assert on_disk == {"HomeNet": "secret-one", "Cafe 5G": "secret-two"}

    # A fresh read (simulating a restart) sees the same data.
    assert network_service.vault_get("HomeNet") == "secret-one"
    assert network_service.vault_get("Cafe 5G") == "secret-two"
    assert network_service.vault_get("Unknown") is None


def test_vault_write_is_atomic_and_leaves_no_temp_files():
    network_service.vault_put("A", "password-a")
    network_service.vault_put("B", "password-b")
    leftovers = [p for p in _vault_file().parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_failed_vault_write_keeps_the_previous_file(monkeypatch):
    network_service.vault_put("HomeNet", "original-pass")

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(network_service.os, "replace", boom)
    with pytest.raises(OSError):
        network_service.vault_put("HomeNet", "new-pass")
    # Restore only os.replace: undo() would also drop the ALTRON_DATA_DIR override.
    monkeypatch.setattr(network_service.os, "replace", real_replace)

    assert network_service.vault_get("HomeNet") == "original-pass"
    assert [p for p in _vault_file().parent.iterdir() if p.suffix == ".tmp"] == []


def test_corrupt_vault_is_moved_aside_not_overwritten():
    _vault_file().parent.mkdir(parents=True, exist_ok=True)
    _vault_file().write_text("{not json", encoding="utf-8")

    assert network_service.vault_get("HomeNet") is None
    assert not _vault_file().exists()
    asides = [p for p in _vault_file().parent.iterdir() if ".corrupt-" in p.name]
    assert len(asides) == 1
    assert asides[0].read_text(encoding="utf-8") == "{not json"


# --- wifi_get_password ---------------------------------------------------------

def test_get_password_parses_key_content_and_stores_it(monkeypatch):
    netsh = FakeNetsh({"Wi-Fi-HomeNet": PROFILE_WITH_KEY})
    monkeypatch.setattr(network_service, "_run", netsh)

    assert network_service.wifi_get_password("Wi-Fi-HomeNet") == "c0lon:in:pass"
    assert netsh.calls == [["netsh", "wlan", "show", "profile", 'name="Wi-Fi-HomeNet"', "key=clear"]]
    assert network_service.vault_get("Wi-Fi-HomeNet") == "c0lon:in:pass"


def test_get_password_open_network_raises_and_stores_nothing(monkeypatch):
    netsh = FakeNetsh({"Cafe 5G": PROFILE_OPEN})
    monkeypatch.setattr(network_service, "_run", netsh)

    with pytest.raises(NetworkError, match="did not return a password"):
        network_service.wifi_get_password("Cafe 5G")
    assert network_service.vault_get("Cafe 5G") is None


def test_get_password_unknown_profile_is_reported(monkeypatch):
    monkeypatch.setattr(network_service, "_run", FakeNetsh({}))
    with pytest.raises(NetworkError, match="not found"):
        network_service.wifi_get_password("Nowhere")


def test_profile_password_parser_keeps_spaces_and_colons():
    assert network_service._profile_password("    Key Content            : a b:c  d\r\n") == "a b:c  d"
    assert network_service._profile_password("Security key : Absent") is None


# --- wifi_forget -----------------------------------------------------------------

def test_forget_backs_up_password_before_deleting(monkeypatch):
    netsh = FakeNetsh({"HomeNet": PROFILE_WITH_KEY.replace("Wi-Fi-HomeNet", "HomeNet")})
    monkeypatch.setattr(network_service, "_run", netsh)

    result = network_service.wifi_forget("HomeNet")

    assert result["backed_up"] is True and result["reason"] == ""
    assert network_service.vault_get("HomeNet") == "c0lon:in:pass"
    verbs = netsh.verbs()
    assert verbs.index("wlan show profile") < verbs.index("wlan delete profile")
    assert netsh.calls[-1] == ["netsh", "wlan", "delete", "profile", 'name="HomeNet"']


def test_forget_without_readable_password_still_deletes_and_says_so(monkeypatch):
    netsh = FakeNetsh({"Cafe 5G": PROFILE_OPEN})
    monkeypatch.setattr(network_service, "_run", netsh)

    result = network_service.wifi_forget("Cafe 5G")

    assert result["backed_up"] is False and "did not return a password" in result["reason"]
    assert netsh.calls[-1][:4] == ["netsh", "wlan", "delete", "profile"]
    assert network_service.vault_get("Cafe 5G") is None


def test_forget_keeps_profile_when_backup_cannot_be_written(monkeypatch):
    netsh = FakeNetsh({"HomeNet": PROFILE_WITH_KEY.replace("Wi-Fi-HomeNet", "HomeNet")})
    monkeypatch.setattr(network_service, "_run", netsh)

    def broken_vault(ssid, password):
        raise OSError("read-only")
    monkeypatch.setattr(network_service, "vault_put", broken_vault)

    with pytest.raises(OSError):
        network_service.wifi_forget("HomeNet")
    assert not any(v == "wlan delete profile" for v in netsh.verbs())


def test_forget_keeps_an_existing_vault_entry_when_password_unreadable(monkeypatch):
    network_service.vault_put("Cafe 5G", "kept-password")
    monkeypatch.setattr(network_service, "_run", FakeNetsh({"Cafe 5G": PROFILE_OPEN}))

    network_service.wifi_forget("Cafe 5G")

    assert network_service.vault_get("Cafe 5G") == "kept-password"


# --- wifi_connect_new ------------------------------------------------------------

def test_connect_new_with_explicit_password_adds_connects_and_cleans_up(monkeypatch):
    netsh = FakeNetsh()
    monkeypatch.setattr(network_service, "_run", netsh)

    name = network_service.wifi_connect_new("Office & Lab", "correct horse 9")

    assert name == "Office & Lab"
    # The XML netsh read was complete and carried both values, escaped.
    xml = netsh.added_xml[0]
    root = ET.fromstring(xml)
    assert root.find(f"{NS}name").text == "Office & Lab"
    assert root.find(f"{NS}MSM/{NS}security/{NS}sharedKey/{NS}keyMaterial").text == "correct horse 9"
    # The temp XML is gone right after netsh read it.
    assert not os.path.exists(netsh.added_paths[0])
    assert netsh.verbs()[0] == "wlan add profile"
    assert netsh.verbs()[-1].startswith("wlan connect")
    assert netsh.calls[-1] == ["netsh", "wlan", "connect", 'name="Office & Lab"']
    assert network_service.vault_get("Office & Lab") == "correct horse 9"


def test_connect_new_temp_file_removed_even_when_netsh_fails(monkeypatch):
    seen = []

    def failing(argv):
        if argv[:4] == ["netsh", "wlan", "add", "profile"]:
            seen.append(argv[4].split('filename="', 1)[1].rstrip('"'))
            raise NetworkError("Access is denied")
        return ""
    monkeypatch.setattr(network_service, "_run", failing)

    with pytest.raises(NetworkError):
        network_service.wifi_connect_new("HomeNet", "password123")
    assert seen and not os.path.exists(seen[0])
    assert network_service.vault_get("HomeNet") is None   # nothing saved for a failed add


def test_connect_new_reconnects_from_vault_when_no_password_given(monkeypatch):
    network_service.vault_put("HomeNet", "vault-password")
    netsh = FakeNetsh()
    monkeypatch.setattr(network_service, "_run", netsh)

    assert network_service.wifi_connect_new("HomeNet") == "HomeNet"

    root = ET.fromstring(netsh.added_xml[0])
    assert root.find(f"{NS}MSM/{NS}security/{NS}sharedKey/{NS}keyMaterial").text == "vault-password"
    assert netsh.calls[-1] == ["netsh", "wlan", "connect", 'name="HomeNet"']


def test_connect_new_without_password_or_vault_entry_does_nothing(monkeypatch):
    netsh = FakeNetsh()
    monkeypatch.setattr(network_service, "_run", netsh)

    with pytest.raises(NetworkError, match="No password is stored"):
        network_service.wifi_connect_new("NeverSaved")
    assert netsh.calls == []


def test_explicit_password_replaces_the_vault_entry(monkeypatch):
    network_service.vault_put("HomeNet", "old-password")
    monkeypatch.setattr(network_service, "_run", FakeNetsh())

    network_service.wifi_connect_new("HomeNet", "new-password")

    assert network_service.vault_get("HomeNet") == "new-password"


# --- handler behaviour -------------------------------------------------------------

def test_get_password_handler_returns_password_but_keeps_it_out_of_the_message(monkeypatch):
    monkeypatch.setattr(network_service, "_run", FakeNetsh({"HomeNet": PROFILE_WITH_KEY.replace("Wi-Fi-HomeNet", "HomeNet")}))

    result = tools_mod.handle_network_control({"action": "wifi_get_password", "ssid": "HomeNet"})

    assert result.success is True
    assert result.output["password"] == "c0lon:in:pass"
    assert "c0lon" not in result.output["message"]   # the message is spoken aloud


def test_connect_new_handler_redacts_password_from_errors(monkeypatch):
    def leaky(argv):
        if argv[:4] == ["netsh", "wlan", "add", "profile"]:
            raise NetworkError("netsh echoed the key hunter2024")
        return ""
    monkeypatch.setattr(network_service, "_run", leaky)

    result = tools_mod.handle_network_control({"action": "wifi_connect_new", "ssid": "HomeNet",
                                               "password": "hunter2024"})

    assert result.success is False
    assert "hunter2024" not in result.error


def test_forget_handler_reports_backup_status(monkeypatch):
    monkeypatch.setattr(network_service, "_run", FakeNetsh({"Cafe 5G": PROFILE_OPEN}))

    result = tools_mod.handle_network_control({"action": "wifi_forget", "ssid": "Cafe 5G"})

    assert result.success is True
    assert result.output["backed_up"] is False
    assert "not backed up" in result.output["message"]


def test_new_actions_are_in_the_tool_schema():
    from src.agent.tools import create_default_registry
    registry = create_default_registry()
    schema = {s["name"]: s for s in registry.get_tools_schema()}["network_control"]["input_schema"]
    enum = schema["properties"]["action"]["enum"]
    assert {"wifi_get_password", "wifi_forget", "wifi_connect_new"} <= set(enum)
    assert "password" in schema["properties"]
