"""Pairing and keys, without a database: codes, scopes, what a device may say about itself."""
from app import auth


def test_codes_are_easy_to_read_and_compare():
    c = auth.new_code()
    assert len(c) == 7 and c[3] == "-" and not set(c.replace("-", "")) & set("01OIL")
    assert auth.norm_code("k7p 4qx") == "K7P-4QX" == auth.norm_code("K7P-4QX") and auth.norm_code("K7P") == ""


def test_keys_are_stored_as_hashes():
    k = auth.new_key()
    assert k.startswith("aur_") and len(k) > 40 and auth.digest(k) != k and len(auth.digest(k)) == 64


def test_each_kind_of_key_reaches_only_its_part():
    assert auth.allowed("browser", "/mailbox/3/decide") and auth.allowed("browser", "/devices/2/revoke")
    assert auth.allowed("node", "/nodes/hello") and not auth.allowed("node", "/mailbox/3/decide")
    assert not auth.allowed("node", "/devices/2/revoke") and not auth.allowed("node", "/ask")
    assert auth.allowed("terminal", "/ask") and auth.allowed("terminal", "/ask/confirm")
    assert not auth.allowed("terminal", "/mailbox") and not auth.allowed("terminal", "/askance")


def test_what_a_device_says_about_itself_is_trimmed():
    info = auth.clean_info({"os": "Windows 11", "nested": {"x": 1}, "long": "y" * 999, **{f"k{i}": i for i in range(40)}})
    assert info["os"] == "Windows 11" and "nested" not in info and len(info["long"]) == 200 and len(info) <= 20
    assert auth.clean_info("nope") == {} and auth.clean_name("  a   b ", "x") == "a b" and auth.clean_name("", "x") == "x"


def test_a_screen_shows_her_but_decides_nothing():
    for path in ("/view", "/ws", "/ask", "/glance", "/tasks", "/mailbox", "/mailbox/3"):
        assert auth.allowed("screen", path), path
    for path in ("/mailbox/3/decide", "/mailbox/decide-batch", "/devices", "/devices/2/revoke", "/devices/2/rename",
                 "/nodes/hello", "/nodes/commands"):
        assert not auth.allowed("screen", path), path
