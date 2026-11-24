# Celery: Redis or RabbitMQ
Using Redis, as RabbitMQ requires setup and config. Since this is not a production-grade project, using Redis for its easier set-up and low latency for testing is preferred. Redis does not ensure delivery of messages either, which would be a consideration for the production version.

# Docker
Opting to use docker for setting up this project, as it allows easier set-up across any machine that downloads this project, given they have access to docker. This project uses a Postgres database and Redis in Celery for message brokerage, which means installation of such dependency on the host machine. Having docker makes the installation and running of this project much quicker and simpler.

For the production-grade version, using a cloud service to host a relational database would be ideal, such as AWS or supabase, depending on policies of the data collected. Other considerations include locations of retailers, volume of data (read and write volume), agreements with customers, and budget.

# Using Meter rather than MeterReading for storing discrepancies
In the case of using Meter, there might be missing MeterReadings. An issue with a MeterReading is the discrepancy, so storing against a MeterReading key isn't reliable. If a Meter doesn't exist, it doesn't get meter readings, so no discrepancies can be recorded against its readings anyway.

# RawReading table
Using a raw meter reading table, we can save and refer to them to ensure we don't miss meter readings that are malformed or don't exist. The trade-off is that when it has tens of millions of readings, it can start to become something to plan for, reaching to tens of gigabytes. Policies that allow old readings to be moved to cheaper storage options, or deleted, can help alleviate this. We could also use a data lake to capture the incoming data, and then process into a data warehouse that is then pulled in by the dashboard.

We can also use an ingesting table, for the case of duplicated readings. Reducing duplicated entries means not having redundant data that isn't used, saving more space and cost.