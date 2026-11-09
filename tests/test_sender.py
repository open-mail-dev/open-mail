import sender
import stores

S = stores.get("outlet.example.com")


class FakeSES:
    def __init__(self):
        self.calls = []

    def send_email(self, **kw):
        self.calls.append(kw)
        return {"MessageId": "ses-1"}


class R:
    subject, preheader, html, text = "Hoi", "pre", "<p>x</p>", "x"


def test_rfc8058_headers():
    h = sender.build_headers("https://mail.example/u/tok", S)
    assert h["List-Unsubscribe"] == "<https://mail.example/u/tok>"
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_send_uses_store_sender_and_headers():
    ses = FakeSES()
    assert sender.send(ses, S, "jan@example.org", R(), "https://mail.example/u/tok", "mail-example-outlet") == "ses-1"
    kw = ses.calls[0]
    assert kw["FromEmailAddress"] == "Example Outlet <hello@mail.outlet.example.com>"
    assert kw["ReplyToAddresses"] == ["hello@outlet.example.com"]
    names = {h["Name"] for h in kw["Content"]["Simple"]["Headers"]}
    assert {"List-Unsubscribe", "List-Unsubscribe-Post"} <= names
