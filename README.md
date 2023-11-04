# Windspeeds

this python script crawls websites and pushes them to windguru.cz.
The documentation of the upload API is available at <https://stations.windguru.cz/upload_api.php>

## create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## crontab

this is an example crontab::

```bash
*/5 * * * * /home/admin/sandbox/windspeed/windguru.sh rohrspitz
*/2* ** */home/admin/sandbox/windspeed/windguru.sh kressbronn
*/10 ** ** /home/admin/sandbox/windspeed/windguru.sh rohrspitz-zamg
```

## passwords

configure the passwords for the windguru upload API in the .env file.
An example file can be found in in the .venv.example file
