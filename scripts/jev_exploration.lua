-- Pure navigation policy: no game input, RPCs, clock reads or hidden decisions.
local M = {}

function M.count(memory, group, reason)
    memory.diagnostics = memory.diagnostics or {}
    local counts = memory.diagnostics[group] or {}
    memory.diagnostics[group] = counts
    counts[reason] = (counts[reason] or 0) + 1
end

local function key(x, z) return tostring(x) .. ":" .. tostring(z) end
local function distance(ax, az, bx, bz)
    return math.sqrt((bx - ax)^2 + (bz - az)^2)
end

function M.path_waypoint(path, px, pz, arrival, lookahead, safe_segment)
    -- The nearest grid-cell center can be BEHIND the player. Prefer the
    -- furthest visible point along the ordered path, not its BFS root.
    for index = #path, 1, -1 do
        local point = path[index]
        local remaining = distance(px, pz, point.x, point.z)
        if remaining > arrival and remaining <= lookahead
            and safe_segment(px, pz, point.x, point.z) then
            return point
        end
    end
    return nil
end

function M.select_gateway(candidates, previous)
    local best = nil
    for _, route in ipairs(candidates) do
        if route.gateway_id == previous then return route end
        if best == nil or route.distance < best.distance
            or (route.distance == best.distance and route.gateway_id < best.gateway_id) then
            best = route
        end
    end
    return best
end

function M.commit_route(memory, source, destination, gateway, first, now, px, pz, remaining, entrance)
    if not gateway or not first then return end
    local route = memory.route_intent
    if not route or route.source ~= source or route.destination ~= destination
        or route.gateway_id ~= gateway or route.first ~= first then
        route = {source=source,destination=destination,gateway_id=gateway,first=first}
        memory.route_intent = route
    end
    route.entrance = entrance or route.entrance
    if not route.last_position or distance(px,pz,route.last_position.x,route.last_position.z)>8 then
        -- A local collection detour is not evidence of oscillating navigation.
        route.progress_anchor,route.elapsed,route.travelled = remaining,0,0
    end
    route.walking,route.sample_at,route.last_position = true,now,{x=px,z=pz}
end

function M.pause_route(memory)
    if memory.route_intent then
        memory.route_intent.walking = false
        memory.route_intent.confirmation_since = nil
    end
end

function M.route_progress(memory, first, now, px, pz)
    local route = memory.route_intent
    if not route then return end
    if not first or first.gateway_id ~= route.gateway_id then return "route_gateway_unavailable" end
    if not route.walking then route.sample_at=now; return end
    local previous = route.last_position
    local moved = previous and distance(px,pz,previous.x,previous.z) or 0
    local elapsed = math.max(0,now-(route.sample_at or now))
    route.last_position,route.sample_at = {x=px,z=pz},now
    if (first.leg or {}).awaiting_region then
        route.confirmation_since=route.confirmation_since or now
        if now-route.confirmation_since>=3 then return "region_confirmation_timeout" end
        return
    end
    route.confirmation_since=nil
    local remaining = first.remaining_distance
    if type(remaining) ~= "number" then return end
    if not route.progress_anchor or moved>12 or remaining<=route.progress_anchor-2 then
        route.progress_anchor,route.elapsed,route.travelled = remaining,0,0
    else
        route.elapsed = (route.elapsed or 0)+math.min(elapsed,2)
        route.travelled = (route.travelled or 0)+moved
    end
    if (route.elapsed or 0)>=12 and ((route.travelled or 0)>=16 or (route.travelled or 0)<1) then
        return "route_no_progress"
    end
end

function M.pause_gateway(memory, gateway, now, reason)
    memory.route_pauses = memory.route_pauses or {}
    local pause = {gateway_id=gateway,until_time=now+30,reason=reason}
    memory.route_pauses[gateway],memory.last_route_pause = pause,pause
    memory.route_intent = nil
    M.count(memory,"graph_paths",reason)
end

function M.gateway_crossing(crossing, px, pz, ax, az, bx, bz, arrival)
    -- Once across the source-side threshold, never steer back to that side.
    -- A collection detour far from the entrance starts a fresh approach.
    local from_side = distance(px, pz, ax, az)
    if from_side > 8 then crossing = false end
    local dx, dz = bx - ax, bz - az
    local length = math.sqrt(dx*dx + dz*dz)
    local projection = length > 0 and ((px-ax)*dx + (pz-az)*dz)/length or -1
    local lateral = length > 0 and math.abs((px-ax)*dz - (pz-az)*dx)/length or math.huge
    return crossing or from_side <= arrival
        or (projection >= 0 and projection <= length + arrival and lateral <= arrival)
end

function M.gateway_destination(ax, az, bx, bz, extension)
    -- A coordinate arrival radius is not proof of entering the next room.
    -- Use a fixed, bounded continuation, never a target that creeps ahead.
    local length = distance(ax, az, bx, bz)
    if length == 0 then return {x=bx,z=bz} end
    return {x=bx+(bx-ax)/length*extension, z=bz+(bz-az)/length*extension}
end

function M.gateway_reached(px, pz, ax, az, bx, bz, arrival)
    if distance(px,pz,bx,bz) <= arrival then return true end
    local dx,dz=bx-ax,bz-az
    local length=math.sqrt(dx*dx+dz*dz)
    return length > 0 and (px-bx)*dx+(pz-bz)*dz >= 0
        and math.abs((px-ax)*dz-(pz-az)*dx)/length <= 1.5
end

function M.frontier_gains(memory, region, neighbors)
    -- Unlabelled but observed land is a transit corridor, not endlessly new
    -- information. Follow it to genuinely unobserved space, without crossing
    -- another known topology room or assigning a fabricated region ID.
    local queue, seen, head, gains = {}, {}, 1, {}
    if region == nil then return gains end
    for id, cell in pairs(memory.cells) do
        if cell.passable and cell.region == region then
            seen[id] = true
            table.insert(queue, id)
        end
    end
    while head <= #queue do
        local id = queue[head]
        head = head + 1
        local cell, gain = memory.cells[id], 0
        for _, offset in ipairs(neighbors) do
            local next_id = key(cell.gx + offset[1], cell.gz + offset[2])
            local next_cell = memory.cells[next_id]
            if next_cell == nil then gain = gain + 1
            elseif next_cell.passable and next_cell.region == nil and not seen[next_id] then
                seen[next_id] = true
                table.insert(queue, next_id)
            end
        end
        if gain > 0 then gains[id] = gain end
    end
    return gains
end

function M.summarize(memory, cell_size, neighbors)
    local ids = {}
    for id, node in pairs(memory.graph_nodes) do
        table.insert(ids, id)
        node.observed_cells, node.unknown_boundary_cells = 0, 0
        node.terrain = {}
    end
    for _, cell in pairs(memory.cells) do
        local node = cell.region and memory.graph_nodes[cell.region]
        if node and cell.passable then
            node.observed_cells = node.observed_cells + 1
            local terrain = cell.biome or "unclassified"
            node.terrain[terrain] = (node.terrain[terrain] or 0) + 1
        end
    end
    table.sort(ids)
    for _, id in ipairs(ids) do
        local node = memory.graph_nodes[id]
        for _ in pairs(M.frontier_gains(memory, id, neighbors)) do
            node.unknown_boundary_cells = node.unknown_boundary_cells + 1
        end
        local best, count, second, total = "unidentified", 0, 0, 0
        for terrain, n in pairs(node.terrain) do
            if terrain ~= "unclassified" then
                total = total + n
                if n > count or (n == count and terrain < best) then
                    second, count, best = count, n, terrain
                else second = math.max(second, n) end
            end
        end
        if total > 0 and second / total >= 0.20 then node.mixed = true end
        local category = node.mixed and "mixed" or best
        node.names = node.names or {}
        if not node.names[category] then
            memory.biome_numbers[category] = (memory.biome_numbers[category] or 0) + 1
            node.names[category] = category .. " " .. tostring(memory.biome_numbers[category])
        end
        node.name, node.biome = node.names[category], category
        node.explored_area = node.observed_cells * cell_size * cell_size
        -- Grid-relative completeness, never inferred from failed pathfinding.
        node.boundary_complete = node.observed_cells > 0 and node.unknown_boundary_cells == 0
    end
end

function M.destinations(memory, current_id, immediate)
    local adjacency = {}
    for _, entrances in pairs(memory.graph_edges) do
        local edge = entrances[1]
        if edge then
            adjacency[edge.a_id] = adjacency[edge.a_id] or {}
            adjacency[edge.b_id] = adjacency[edge.b_id] or {}
            table.insert(adjacency[edge.a_id], edge.b_id)
            table.insert(adjacency[edge.b_id], edge.a_id)
        end
    end
    for _, neighbors in pairs(adjacency) do table.sort(neighbors) end
    local queue, seen, head, result = {}, { [current_id] = true }, 1, {}
    local roots = {}
    for id in pairs(immediate) do table.insert(roots, id) end
    table.sort(roots, function(a, b)
        return immediate[a].distance == immediate[b].distance and a < b
            or immediate[a].distance < immediate[b].distance
    end)
    for _, id in ipairs(roots) do
        seen[id] = true
        table.insert(queue, { id = id, first = id, hops = 1 })
    end
    while head <= #queue do
        local entry = queue[head]
        head = head + 1
        local node, first = memory.graph_nodes[entry.id], immediate[entry.first]
        if node then
            local resources = {}
            for prefab in pairs(node.resources) do table.insert(resources, prefab) end
            table.sort(resources)
            table.insert(result, {
                status = "ready", node_id = entry.id, name = node.name,
                topology = node.region, room_type = string.match(node.region or entry.id,"([^:]+)$"),
                biome = node.biome, terrain = node.terrain,
                explored_area = node.explored_area, observed_cells = node.observed_cells,
                boundary_complete = node.boundary_complete,
                unknown_boundary_cells = node.unknown_boundary_cells,
                entered = node.entries > 0, resources_seen = resources,
                hops = entry.hops, next_node_id = entry.first,
                gateway_id = first.gateway_id,
                source_node_id = first.source_node_id, entrance = first.entrance,
                -- Distance is to the FIRST exit, not a fabricated full route length.
                distance = first.distance, remaining_distance = first.remaining_distance,
                target = first.target, leg = first.leg,
            })
        end
        for _, id in ipairs(adjacency[entry.id] or {}) do
            if not seen[id] then
                seen[id] = true
                table.insert(queue, { id = id, first = entry.first, hops = entry.hops + 1 })
            end
        end
    end
    -- Keep the first hop of the route actually being executed, even when a
    -- shorter alternative reappears. Other destinations retain ordinary BFS.
    local locked = memory.route_intent
    local first = locked and locked.walking and immediate[locked.first]
    if first and first.gateway_id==locked.gateway_id then
        local pending,visited,index = {{id=locked.first,hops=1}},{[current_id]=true,[locked.first]=true},1
        while index<=#pending do
            local entry=pending[index]; index=index+1
            if entry.id==locked.destination then
                for _, route in ipairs(result) do
                    if route.node_id==locked.destination then
                        route.hops,route.next_node_id,route.gateway_id = entry.hops,locked.first,first.gateway_id
                        route.distance,route.remaining_distance = first.distance,first.remaining_distance
                        route.target,route.leg = first.target,first.leg
                        route.source_node_id,route.entrance = first.source_node_id,first.entrance
                        break
                    end
                end
                break
            end
            for _, id in ipairs(adjacency[entry.id] or {}) do
                if not visited[id] then
                    visited[id]=true; pending[#pending+1]={id=id,hops=entry.hops+1}
                end
            end
        end
    end
    table.sort(result, function(a, b)
        return a.hops == b.hops and (a.distance == b.distance and a.node_id < b.node_id
            or a.distance < b.distance) or a.hops < b.hops
    end)
    return result
end

function M.reject(memory, now)
    local search = memory.searches[memory.current_region]
    if search and search.target then
        search.pauses[search.target.key] = now + 30
        search.target = nil
        search.mode = "reconsider_direction"
    end
end

function M.search(memory, candidates, area, px, pz, now, make_leg, safe_segment)
    if not area then return { status = "unknown_region" } end
    if area.boundary_complete then return { status = "boundary_complete" } end
    local search = memory.searches[area.id]
    if not search then
        search = { pauses = {}, mode = "directional", coast_side = 1,
            origin_x = px, origin_z = pz, travelled = 0, history = {} }
        memory.searches[area.id] = search
    end
    if search.last_x then
        search.travelled = search.travelled + distance(search.last_x, search.last_z, px, pz)
    end
    search.last_x, search.last_z = px, pz
    local lookup = {}
    for _, candidate in ipairs(candidates) do lookup[candidate.key] = candidate end
    if search.target and (not lookup[search.target.key]
        or distance(px, pz, search.target.x, search.target.z) <= 1.5) then
        search.target = nil
    end
    local history = search.history
    local last = history[#history]
    if not last or distance(last.x, last.z, px, pz) >= 2 then
        local loop = false
        for i = 1, #history - 3 do
            if distance(history[i].x, history[i].z, px, pz) < 1.5 then loop = true end
        end
        if loop then
            if search.target then search.pauses[search.target.key] = now + 30 end
            search.target = nil
            search.mode = "reconsider_direction"
            search.hx, search.hz = nil, nil
            history = {}
            search.history = history
        end
        table.insert(history, { x = px, z = pz })
        if #history > 32 then table.remove(history, 1) end
    end
    if not search.hx and #candidates > 0 then
        local first = candidates[1]
        local length = distance(px, pz, first.x, first.z)
        search.hx, search.hz = (first.x - px)/length, (first.z - pz)/length
    end
    if not search.hx then
        M.count(memory, "exit_search", "no_eligible_frontier")
        return { status = "temporarily_blocked", boundary_complete = false }
    end
    local forward = safe_segment(px, pz, px + search.hx*6, pz + search.hz*6)
    -- A short obstruction does not immediately count as coastline: require a
    -- laterally blocked sample too. Keep the chosen side until forward opens.
    local lx, lz = -search.hz*2, search.hx*2
    local wide_blocked = not forward
        and not safe_segment(px + lx, pz + lz, px + lx + search.hx*6, pz + lz + search.hz*6)
        and not safe_segment(px - lx, pz - lz, px - lx + search.hx*6, pz - lz + search.hz*6)
    if wide_blocked then
        if search.mode ~= "coast" then
            local left = safe_segment(px, pz, px - search.hz*4, pz + search.hx*4)
            search.coast_side = left and 1 or -1
        end
        search.mode = "coast"
    elseif forward then search.mode = "directional" end

    local ranked = {}
    for _, candidate in ipairs(candidates) do
        if (search.pauses[candidate.key] or 0) <= now then
            local dx, dz = candidate.x - px, candidate.z - pz
            local length = math.max(1, distance(px, pz, candidate.x, candidate.z))
            local projection = (dx*search.hx + dz*search.hz)/length
            local lateral = (-dx*search.hz + dz*search.hx)/length
            local score = projection*30 + candidate.information_gain*3
                - (candidate.visits or 0)*12 - length*0.10
            if search.mode == "coast" then score = score + lateral*search.coast_side*35 end
            if search.target and candidate.key == search.target.key then score = score + 20 end
            table.insert(ranked, { candidate = candidate, score = score })
        end
    end
    table.sort(ranked, function(a, b)
        return a.score == b.score and a.candidate.key < b.candidate.key or a.score > b.score
    end)
    local chosen, leg
    for index, entry in ipairs(ranked) do
        if index > 24 then break end -- bound expensive reachability probes per frame
        local possible = make_leg(entry.candidate)
        if possible and safe_segment(px, pz, possible.x, possible.z) then
            chosen, leg = entry.candidate, possible
            break
        end
    end
    if not chosen then
        M.count(memory, "exit_search", #ranked == 0 and "all_candidates_paused" or "no_safe_frontier_path")
        return { status = "temporarily_blocked", boundary_complete = false }
    end
    M.count(memory, "exit_search", "ready")
    search.target = chosen
    local strategy = search.mode
    search.strategy = strategy
    return { status = "ready", region_id = area.id, strategy = strategy,
        heading = { x = search.hx, z = search.hz }, travelled = search.travelled,
        boundary_complete = false, unknown_boundary_cells = area.unknown_boundary_cells,
        target = { x = chosen.x, z = chosen.z, distance = distance(px, pz, chosen.x, chosen.z) },
        leg = { x = leg.x, z = leg.z, distance = leg.distance, detour = leg.detour == true } }
end

return M
