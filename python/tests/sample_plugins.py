from cordis import plugin

events = []


@plugin
def configured(_ctx, config):
    events.append(("start", config))
    return lambda: events.append(("stop", config))
