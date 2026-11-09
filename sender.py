def build_headers(unsubscribe_url: str, store) -> dict:
    # RFC 8058 one-click unsubscribe only requires the https URI — a mailto
    # fallback here would go to an address we don't receive, silently losing
    # unsubscribes.
    return {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def send(ses_client, store, to: str, rendered, unsubscribe_url: str, configuration_set: str) -> str:
    headers = [{"Name": k, "Value": v} for k, v in build_headers(unsubscribe_url, store).items()]
    res = ses_client.send_email(
        FromEmailAddress=f"{store.from_name} <{store.from_email}>",
        ReplyToAddresses=[store.reply_to],
        Destination={"ToAddresses": [to]},
        ConfigurationSetName=configuration_set,
        Content={"Simple": {
            "Subject": {"Data": rendered.subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": rendered.html, "Charset": "UTF-8"},
                     "Text": {"Data": rendered.text, "Charset": "UTF-8"}},
            "Headers": headers,
        }},
    )
    return res["MessageId"]
