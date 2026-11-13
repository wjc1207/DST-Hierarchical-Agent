"""Validated local-player autonomy settings shared by bridge and host."""


def settings(state, *, companion=False):
    value = state.get("autonomy")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid autonomy settings")
    enabled, revision, goal = value.get("enabled"), value.get("revision"), value.get("goal")
    if type(enabled) is not bool or type(revision) is not int or not 0 <= revision <= 2**53 - 1:
        raise ValueError("Invalid autonomy enabled/revision")
    if goal is not None and (
        not isinstance(goal, str) or not goal.strip() or len(goal) > 256
        or len(goal.encode("utf-8")) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in goal)
    ):
        raise ValueError("Invalid long-term goal")
    result = {"enabled": enabled, "revision": revision, "goal": goal}
    if "mode" in value:
        if not isinstance(value["mode"], str) or value["mode"] not in {"instruction", "autonomous"}:
            raise ValueError("Invalid agent mode")
        messages = value.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 8:
            raise ValueError("Invalid player messages")
        dialogue_revision = value.get("dialogue_revision") if companion else None
        if dialogue_revision is not None:
            if type(dialogue_revision) is not int or not 0 < dialogue_revision <= 2**53 - 1:
                raise ValueError("Invalid companion dialogue revision")
            result["dialogue_revision"] = dialogue_revision
        message_limit = dialogue_revision if dialogue_revision is not None else revision
        last_id = 0
        for message in messages:
            if (not isinstance(message, dict) or type(message.get("id")) is not int
                    or not last_id < message["id"] <= message_limit):
                raise ValueError("Invalid player message ID")
            validate_text(message.get("text"))
            last_id = message["id"]
        if dialogue_revision is not None and enabled and last_id != dialogue_revision:
            raise ValueError("Missing latest companion dialogue")
        instruction = value.get("instruction")
        if instruction is not None:
            validate_text(instruction)
        result.update(mode=value["mode"], messages=messages, instruction=instruction)
    return result


def validate_text(text):
    if (not isinstance(text, str) or not text.strip() or len(text) > 256
            or len(text.encode("utf-8")) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in text)):
        raise ValueError("Invalid agent message (1..256 characters, single line)")
    return text


def enabled(state):
    control = settings(state)
    return control is None or control["enabled"]


def revision(state):
    control = settings(state)
    return None if control is None else control["revision"]
