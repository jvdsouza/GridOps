![domain model](domain-model.svg)

| Entity | Responsibility |
| --- | --- |
| Customer | Represents the account receiving electricity service |
| Meter | Identifies the device supplying usage data and its expected reading interval |
| RawReading | Records every reading submission exactly as received, before validation |
| MeterReading | Stores the electricity consumed during a specific interval |
| BillingPeriod | Defines the time window over which usage is aggregated |
| Discrepancy | Tracks a detected data problem requiring investigation |

For V1, assume: 
- each meter belongs to one customer, 
- reports consumption in kWh every 30 minutes, 
- and that all readings are interval consumption values rather than cumulative meter total

A meter should have at most one accepted reading for a given interval start.
Usage cannot be negative.
Readings can arrive out of chronological order.
Missing intervals should be detectable.
A billing period uses a start-inclusive, end-exclusive time window.
A duplicate submission should not double-count consumption.
A discrepancy can be open or resolved.
Every submission is kept as a RawReading, whether it is accepted, rejected or a duplicate, so nothing received is lost.

One customer has One-to-Many meter
One Meter has On-To-Many MeterReading
One Customer has One-to-Many BillingPeriod
One Meter has One-to-Many Discrepancy
One Meter has One-to-Many RawReading
One MeterReading has One-to-Many RawReading (the submission that created it, plus any duplicates of it)
One RawReading has One-to-Many Discrepancy

A Customer has:
- id
- name (string)

A Meter has:
- id
- customer_id
- interval_minutes (interval)

A MeterReading has:
- id
- meter_id
- interval_start (timestamptz)
- usage (decimal)
- usage_unit (text)

A RawReading has:
- id
- meter_id
- interval_start (timestamptz)
- usage (decimal): as submitted, may be negative
- usage_unit (text): as submitted, e.g. `kWh` or `MW`
- source (text): where it came from, e.g. a file name or `api`
- received_at (timestamptz)
- status (text): `pending` (default), `accepted`, `rejected` or `duplicate`
- rejection_reason (text): required when rejected
- meter_reading_id (nullable): required when accepted or duplicate

A BillingPeriod has:
- id
- customer_id
- start_at (timestamptz)
- end_at (timestamptz)
- status (text)

A Discrepancy has:
- id
- meter_id
- raw_reading_id (nullable): the submission that triggered it; empty for missing readings
- type (text): `missing_reading`, `duplicate_reading` or `suspicious_reading`
- status (text): `open` (default) or `resolved`