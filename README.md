# pybc-relayer

This repo shows how to setup the various IBC components (clients, connections, ports, channels) in python. Both the notebooks and scripts were built and tested within a container defined by files in the docker directory. 

Therefore, if you want to interact with the IBC tutorial notebooks, or run the python scripts, it is recommended to setup/install Docker, build the image, and run a container (http://localhost:6994/lab for jupyter lab). Additionally, please update the creds.json in the scripts directory.

We heavily utilize/reference several projects:

- confio's ts-relayer and simple-ica repos (https://github.com/confio/ts-relayer and https://github.com/confio/cw-ibc-demo)
- terra's python and proto repos (https://github.com/terra-money/terra.py and https://github.com/terra-money/terra.proto)

## Docker

After installing Docker, run build.sh and run.sh to get the pybc-relayer jupyter lab running on http://localhost:6994/lab.

## Notebooks

The notebooks are named in the recommended order of execution. Currently, they walkthru setting up an IBC connection between injective and osmosis testnets, and deploying confio's simple-ica contracts.

The "business-as-usual" relaying is still a WIP, but confio's ts-relayer and hermes are perfectly fine/production-level relayers.

## Scripts

Please fill-in your credentials in creds.json in order to run both the .ipynb and .py files. The setup_*.py files basically are script versions of the notebooks.