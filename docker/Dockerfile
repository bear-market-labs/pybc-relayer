##############################################
# base image
#############################################

from python:3.8-buster

#############################################
# debian libraries
#############################################

run apt-get update && \ 
  DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends \
  vim screen curl unzip

#############################################
# setup volumes
#############################################

run mkdir /repos
volume /repos

#############################################
# python libraries
#############################################

copy ./requirements.txt /opt/

run pip install -r /opt/requirements.txt

#############################################
# command
#############################################

entrypoint ["jupyter", "lab", "--port=6994", "--allow-root", "--NotebookApp.token=''", "--notebook-dir=/repos"]
