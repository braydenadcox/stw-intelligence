# Normalized Mission Feed Contract

STW Intelligence accepts a provider-approved HTTPS JSON feed without coupling the
telemetry parser or database to that provider. The transport sends `Accept:
application/json`, supports an optional environment-backed API-key header, and honors
`ETag` and `Last-Modified`. Responses are limited to 5 MiB.
Redirects are rejected so a configured credential cannot be forwarded to another origin.

The root object uses the same version-one envelope as the permission-safe fixture:

```json
{
  "rotation": {
    "key": "provider-rotation-2026-08-08",
    "valid_from": "2026-08-08T00:00:00Z",
    "valid_until": "2026-08-09T00:00:00Z",
    "source_timestamp": "2026-08-08T00:01:00Z"
  },
  "missions": [
    {
      "provider_mission_key": "provider-node-123",
      "theater": {"code": "twine_peaks", "name": "Twine Peaks"},
      "objective": {
        "code": "ride_the_lightning",
        "name": "Ride the Lightning"
      },
      "power_level": 160,
      "husk_power_level": 250,
      "biome": {"code": "arid_wild_west", "name": "Arid Wild West"},
      "is_four_player": true,
      "alert_type": "mini_boss",
      "rewards": [
        {
          "kind": "alert",
          "item_code": "legendary_survivor",
          "display_name": "Legendary Survivor",
          "rarity": "legendary",
          "quantity": 1
        }
      ],
      "modifiers": [
        {
          "modifier_code": "water_storm",
          "display_name": "Water Storm",
          "element": "water"
        }
      ]
    }
  ]
}
```

Required rotation fields are `key`, `valid_from`, and `valid_until`. The validity
timestamps must be UTC-compatible ISO 8601 values and `valid_from` must precede
`valid_until`.

Each mission requires `theater`, `objective`, and `power_level`. Rewards, modifiers,
biome, map position, husk PL, four-player status, alert type, and provider keys are
optional. Unknown reward kinds are retained as `other`; unknown objective, theater,
biome, reward, and modifier codes remain representable rather than invalidating the
rotation.

Provider identity and terms are local configuration, not trusted from the response.
API keys are never accepted as command-line values, stored in SQLite, logged, or added
to snapshots. HTTP is rejected except when the explicit local-development flag is used.

FortniteDB's documented `GET /missions/summary` response is not compatible with this
contract because it does not contain the complete mission rows required for rotation-aware
correlation.
