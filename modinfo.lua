name = "JEV DST Agent"
description = [[
Exports the local player's structured state to client_log.txt and accepts
semantic actions from the local JEV controller.
]]
author = "Retard Wang"
version = "1.8.8"

icon_atlas = "modicon.xml"
icon = "modicon.tex"

api_version = 10
dst_compatible = true
client_only_mod = true
all_clients_require_mod = false

server_filter_tags = {}

configuration_options =
{
    {
        name = "performance_interval",
        label = "Performance log interval",
        hover = "Seconds between compact client FPS summaries; full state dumps are disabled.",
        options =
        {
            { description = "2 seconds", data = 2.0 },
            { description = "5 seconds", data = 5.0 },
            { description = "10 seconds", data = 10.0 },
        },
        default = 5.0,
    },
    {
        name = "scan_radius",
        label = "Nearby scan radius",
        hover = "World units searched for useful resources, light and danger.",
        options =
        {
            { description = "10", data = 10 },
            { description = "15", data = 15 },
            { description = "20", data = 20 },
            { description = "25", data = 25 }
        },
        default = 15,
    },
}
