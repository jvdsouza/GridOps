# Celery: Redis or RabbitMQ
Using Redis, as RabbitMQ requires setup and config. Since this is not a production-grade project, using Redis for its easier set-up and low latency for testing is preferred. Redis does not ensure delivery of messages either, which would be a consideration for the production version.

# Docker
Opting to use docker for setting up this project, as it allows easier set-up across any machine that downloads this project, given they have access to docker. This project uses a Postgres database and Redis in Celery for message brokerage, which means installation of such dependency on the host machine. Having docker makes the installation and running of this project much quicker and simpler.

For the production-grade version, using a cloud service to host a relational database would be ideal, such as AWS or supabase, depending on policies of the data collected. Other considerations include locations of retailers, volume of data (read and write volume), agreements with customers, and budget.

