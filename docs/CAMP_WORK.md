# Camp storage, cooking and charcoal

DeepSeek submits goals. The native executor chooses current actions for those
goals; camp actions are excluded from JEV's choice list.

| Goal target | Fields | Completion |
| --- | --- | --- |
| `inspect_storage` | `building_id` for a known chest | The chest is open and its slots have been observed. |
| `deposit` / `withdraw` | Chest `building_id`, `prefab`, `count` (1–40) | The requested number of units moved. Each unit is confirmed by changes on both sides. |
| `cook_meal` | Pot `building_id`, `product`, four `ingredients` | A dish from the agent's cooking task has entered inventory. |
| `ignite_tree` | Observed `tree_guid` | The selected tree catches fire. |
| `inventory` | `prefab: "charcoal"`, `count`, optional `allow_ignite: true` | The inventory reaches the target. Collect dropped charcoal or chop burnt trees first; the flag also permits lighting an isolated tree. |

Use `query_places` to find known building IDs. `query_storage` reports current
container observations; closed or out of view means unknown. `query_meals`
returns verified Crock Pot ingredient combinations for meatballs and ratatouille.
Use `query_recipes` to plan construction and crafting intermediates. Build a
chest from three boards; a pot uses three cut stone, six charcoal and six twigs.

The agent can do other work while cooking. Keep the same task ID and target
across replans so it can return for the meal. It leaves untracked meals and
incompatible ingredients in the pot alone. If an issued transfer or harvest
cannot be verified, inspect the building before retrying.

Tree ignition requires an equipped torch, daylight or dusk, a safe approach,
and separation from home, nearby structures, other players and combustibles.
It waits for the tree to burn out before chopping. Prepare an axe to complete
the charcoal goal.

In-game smoke test: deposit five logs in an empty chest and withdraw two. The
chest should contain three. Next, build a pot, cook one meatballs using a
queried recipe, and check that the dish appears in inventory. Test the charcoal
chain on a separate isolated tree during daytime.
