from config import VALID_SERVICES, VALID_PROTOCOLS


class ProtocolError(Exception):
    pass


def parse(raw: bytes):
    """Return (kind, fields_dict) or raise ProtocolError."""
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ProtocolError("not valid UTF-8")
    if not text:
        raise ProtocolError("empty message")

    parts = text.split("|")
    kind = parts[0]

    if kind == "REGISTER":
        if len(parts) != 5:
            raise ProtocolError("REGISTER needs 5 fields: %r" % text)
        _, pid, service, proto, port = parts
        if not pid or "," in pid:
            raise ProtocolError("bad provider id: %r" % pid)
        if service not in VALID_SERVICES:
            raise ProtocolError("unsupported service: %r" % service)
        if proto not in VALID_PROTOCOLS:
            raise ProtocolError("unsupported protocol: %r" % proto)
        try:
            port = int(port)
        except ValueError:
            raise ProtocolError("port is not a number")
        if not 1 <= port <= 65535:
            raise ProtocolError("port out of range")
        return kind, {"provider_id": pid, "service": service,
                      "protocol": proto, "port": port}

    if kind == "HEARTBEAT":
        if len(parts) != 2 or not parts[1]:
            raise ProtocolError("HEARTBEAT needs 2 fields: %r" % text)
        return kind, {"provider_id": parts[1]}

    if kind == "DISCOVER":
        if len(parts) != 2 or parts[1] not in VALID_SERVICES:
            raise ProtocolError("bad DISCOVER: %r" % text)
        return kind, {"service": parts[1]}

    raise ProtocolError("unknown message type: %r" % kind)


def fmt_register_ok(pid, service):
    return ("REGISTER_OK|%s|%s" % (pid, service)).encode()


def fmt_discover_response(service, providers):
    items = ["%s,%s,%s,%d" % (p.provider_id, p.protocol, p.ip, p.port)
             for p in providers]
    return ("DISCOVER_RESPONSE|%s|%s" % (service, "|".join(items))).encode()


def fmt_unavailable(service):
    return ("ERROR|SERVICE_UNAVAILABLE|%s" % service).encode()
