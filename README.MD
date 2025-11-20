## KIA-Live Serverside
#### Takes data from BMTC APIs to provide a semi-live data feed

- ### Setup:
This is expected to be functioning on an AWS EC2 Instance, however it can technically run anywhere. To set it up first install all dependents
via the poetry package manager `poetry install` command. Once installed you can simply run it via 
`poetry run python -m src.main` from the `src` folder. To expose it you can use an nginx reverse proxy, or cloudflared type tunnelling service. It will run on port `59966` to avoid conflicts with other services.

The `generate_in_files.py` generates files to replace in the `in/` directory. Options listed below:

| Option | Note                                                       |
|--------|------------------------------------------------------------|
| -r     | Create routeline.json with API data                        |
| -t     | Create timings.tsv with API data                           |
| -tdb   | Create times.json with live_data.db predictions            |
| -s     | Create client_stops.json with API data                     |
| -c     | Copy all generated files, overwriting existing `in/` files |