#imports

import pandas as pd
import json
import os
import sys
import base64
import requests
import subprocess
import math
import hashlib
import bech32
import time

from dateutil.parser import parse
from datetime import datetime, timedelta
from ecdsa import SECP256k1, SigningKey
from ecdsa.util import sigencode_string_canonize
from bech32 import bech32_decode, bech32_encode, convertbits
from google.protobuf.timestamp_pb2 import Timestamp as googTimestamp

from terra_sdk.client.lcd import LCDClient
from terra_sdk.core.wasm import MsgStoreCode, MsgInstantiateContract, MsgExecuteContract
from terra_sdk.core.bank import MsgSend
from terra_sdk.core.fee import Fee
from terra_sdk.key.mnemonic import MnemonicKey
from terra_sdk.core.bech32 import get_bech
from terra_sdk.core import AccAddress, Coin, Coins
from terra_sdk.client.lcd.api.tx import CreateTxOptions, SignerOptions
from terra_sdk.client.localterra import LocalTerra
from terra_sdk.core.wasm.data import AccessConfig
from terra_sdk.client.lcd.api._base import BaseAsyncAPI, sync_bind

from terra_proto.cosmwasm.wasm.v1 import AccessType
from terra_proto.cosmos.tx.v1beta1 import Tx, TxBody, AuthInfo, SignDoc, SignerInfo, ModeInfo, ModeInfoSingle, BroadcastTxResponse
from terra_proto.cosmos.base.abci.v1beta1 import TxResponse
from terra_proto.cosmos.tx.signing.v1beta1 import SignMode
from terra_proto.ibc.core.client.v1 import MsgCreateClient, Height, MsgUpdateClient, QueryClientStateRequest, QueryClientStateResponse
from terra_proto.ibc.core.channel.v1 import MsgChannelOpenInit, Channel, State, Order, Counterparty, MsgChannelOpenTry, MsgChannelOpenAck, MsgChannelOpenConfirm, QueryUnreceivedPacketsRequest, QueryUnreceivedPacketsResponse, QueryPacketCommitmentRequest, QueryPacketCommitmentResponse, Packet, QueryNextSequenceReceiveRequest, QueryNextSequenceReceiveResponse, MsgRecvPacket, MsgTimeout, QueryUnreceivedAcksRequest, QueryUnreceivedAcksResponse, MsgAcknowledgement
from terra_proto.ibc.core.connection.v1 import MsgConnectionOpenInit, Counterparty as ConnectionCounterParty, Version, MsgConnectionOpenTry, MsgConnectionOpenAck, MsgConnectionOpenConfirm
from terra_proto.ibc.lightclients.tendermint.v1 import ClientState, ConsensusState, Fraction, Header
from terra_proto.ics23 import HashOp, LengthOp, LeafOp, InnerOp, ProofSpec, InnerSpec, CommitmentProof, ExistenceProof, NonExistenceProof, BatchProof, CompressedBatchProof, BatchEntry, CompressedBatchEntry, CompressedExistenceProof, CompressedNonExistenceProof
from terra_proto.ibc.core.commitment.v1 import MerkleRoot, MerklePrefix, MerkleProof
from terra_proto.tendermint.types import ValidatorSet, Validator, SignedHeader, Header as tendermintHeader, Commit, BlockId, PartSetHeader, CommitSig, BlockIdFlag
from terra_proto.tendermint.version import Consensus
from terra_proto.tendermint.crypto import PublicKey
from betterproto.lib.google.protobuf import Any
from betterproto import Timestamp

#misc helper functions
sys.path.append(os.path.join(os.path.dirname(__name__), '..', 'scripts'))

from helpers import proto_to_binary, timestamp_string_to_proto, stargate_msg, create_ibc_client, fetch_chain_objects, bech32_to_hexstring, hexstring_to_bytes, bech32_to_b64, b64_to_bytes, fabricate_update_client, fetch_proofs, deploy_local_wasm, init_contract, execute_msg, fetch_channel_proof

#setup lcd clients, rpc urls, inj_wallets
(inj, inj_wallet, inj_rpc_url, inj_rpc_header) = fetch_chain_objects("injective-888")
(osmo, osmo_wallet, osmo_rpc_url, osmo_rpc_header) = fetch_chain_objects("osmo-test-4")

#load ibc client & connection information from previous notebooks
context = {}
# Open the file for reading
with open("context.json", "r") as f:
    # Load the dictionary from the file
    context = json.load(f)
    
client_id_on_inj = context["client_id_on_inj"]
client_id_on_osmo = context["client_id_on_osmo"]
connection_id_on_inj = context["connection_id_on_inj"]
connection_id_on_osmo = context["connection_id_on_osmo"]

###################################################
#load wasms & instantiate contracts
###################################################

##from scratch code upload
#ica_controller_code_id = deploy_local_wasm("/repos/cw-ibc-demo/artifacts/simple_ica_controller.wasm", inj_wallet, inj)
#osmo_ica_host_code_id = deploy_local_wasm("/repos/cw-ibc-demo/artifacts/simple_ica_host.wasm", osmo_wallet, osmo)
#osmo_cw1_code_id = deploy_local_wasm("/repos/cw-plus/artifacts/cw1_whitelist.wasm", osmo_wallet, osmo)

#using already uploaded code ids
ica_controller_code_id = '536'
osmo_ica_host_code_id = '4692'
osmo_cw1_code_id = '4693'

#controller
init_msg = {
}
controller_result = init_contract(ica_controller_code_id, init_msg, inj_wallet, inj, "ica_controller")
controller_address = controller_result.logs[0].events_by_type["wasm"]["_contract_address"][0]

#host
init_msg = {
  "cw1_code_id": int(osmo_cw1_code_id),
}
host_result = init_contract(osmo_ica_host_code_id, init_msg, osmo_wallet, osmo, "ica_host")
host_address = host_result.logs[0].events_by_type["instantiate"]["_contract_address"][0]

#contracts automatically generate an ibc port
controller_port = inj.wasm.contract_info(controller_address)["ibc_port_id"]
host_port = osmo.wasm.contract_info(host_address)["ibc_port_id"]

print(f"""
ica_controller_code_id on inj: {ica_controller_code_id}
ica_host_code_id on osmo: {osmo_ica_host_code_id}
cw1_code_id on osmo: {osmo_cw1_code_id}

controller_address: {controller_address}
host_address: {host_address}

controller_port: {controller_port}
host_port: {host_port}
""")

###################################################
#create ibc channel
###################################################

#ChannelOpenInit
msg = MsgChannelOpenInit(
  port_id=controller_port,
  channel=Channel(
    state=State.STATE_INIT,
    ordering=Order.ORDER_UNORDERED,
    counterparty=Counterparty(
      port_id=host_port,
    ),
    connection_hops=[
      connection_id_on_inj
    ],
    version="simple-ica-v2",
  ),
  signer=wallet.key.acc_address
)

channel_open_init_on_inj = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenInit", msg, inj_wallet, inj)
channel_open_init_on_inj_df = pd.DataFrame(channel_open_init_on_inj["tx_response"]["logs"][0]["events"][0]["attributes"])
channel_id_on_inj = channel_open_init_on_inj_df[channel_open_init_on_inj_df["key"]=="channel_id"]["value"].values[0]

print(channel_open_init_on_inj_df)

#ChannelOpenTry
time.sleep(10) #wait a few blocks
msg = fabricate_update_client(inj, inj_rpc_url, inj_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", msg, osmo_wallet, osmo)
header_height = Header.FromString(msg.header.value).signed_header.header.height

inj_client_trusted_height = header_height
inj_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

channel_proof = fetch_channel_proof(inj_rpc_url, inj_rpc_header, controller_port, channel_id_on_inj, inj_client_trusted_height, inj_client_trusted_revision_number)

msg = MsgChannelOpenTry(
  port_id=host_port,
  channel=Channel(
    state=State.STATE_TRYOPEN,
    ordering=Order.ORDER_UNORDERED,
    counterparty=Counterparty(
      port_id=controller_port,
      channel_id=channel_id_on_inj,
    ),
    connection_hops=[
      connection_id_on_osmo
    ],
    version="simple-ica-v2",
  ),
  counterparty_version="simple-ica-v2",
  proof_init=channel_proof.SerializeToString(),
  proof_height=Height(int(inj_client_trusted_revision_number), int(inj_client_trusted_height)),
  signer=osmo_wallet.key.acc_address,
)

channel_open_try_result = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenTry", msg, osmo_wallet, osmo)
channel_open_try_on_osmo_df = pd.DataFrame(channel_open_try_result["tx_response"]["logs"][0]["events"][0]["attributes"])
channel_id_on_osmo = channel_open_try_on_osmo_df[channel_open_try_on_osmo_df["key"]=="channel_id"]["value"].values[0]

print(channel_open_try_on_osmo_df)

#ChannelOpenAck
time.sleep(15) #wait a few blocks
update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, inj_rpc_header, inj, inj_wallet, client_id_on_inj)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, inj_wallet, inj)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

osmo_client_trusted_height = header_height
osmo_client_trusted_revision_number = inj.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_inj}")["client_state"]["latest_height"]["revision_number"]

channel_proof = fetch_channel_proof(osmo_rpc_url, inj_rpc_header, host_port, channel_id_on_osmo, osmo_client_trusted_height, osmo_client_trusted_revision_number)

msg = MsgChannelOpenAck(
  port_id=controller_port,
  channel_id=channel_id_on_inj,
  counterparty_channel_id=channel_id_on_osmo,
  counterparty_version="simple-ica-v2",
  proof_try=channel_proof.SerializeToString(),
  proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
  signer=wallet.key.acc_address,
)

channel_open_ack_result = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenAck", msg, inj_wallet, inj)
channel_open_ack_on_inj_df = pd.DataFrame(channel_open_ack_result["tx_response"]["logs"][0]["events"][0]["attributes"])

print(channel_open_ack_on_inj_df)

#parse the ibc who_am_i packet for relay after channel setup is complete
packet_df = pd.DataFrame([x for x in channel_open_ack_result["tx_response"]["logs"][0]["events"] if x["type"]=="send_packet"][0]["attributes"])
packet_df["index"] = 0
packet_df = packet_df.pivot(index="index", columns="key", values="value")
packet_row = packet_df.iloc[0,:]

packet_to_relay = Packet(
  sequence=int(packet_row["packet_sequence"]),
  source_port=packet_row["packet_src_port"],
  source_channel=packet_row["packet_src_channel"],
  destination_port=packet_row["packet_dst_port"],
  destination_channel=packet_row["packet_dst_channel"],
  data=hexstring_to_bytes(packet_row["packet_data_hex"]),
  timeout_height=Height(int(packet_row["packet_timeout_height"].split('-')[0]), int(packet_row["packet_timeout_height"].split('-')[1])),
  timeout_timestamp=int(packet_row["packet_timeout_timestamp"]),
)

print(packet_to_relay)

#ChannelOpenConfirm
time.sleep(10) #wait a few blocks
update_client_msg = fabricate_update_client(inj, inj_rpc_url, inj_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

inj_client_trusted_height = header_height
inj_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

channel_proof = fetch_channel_proof(inj_rpc_url, inj_rpc_header, controller_port, channel_id_on_inj, inj_client_trusted_height, inj_client_trusted_revision_number)

msg = MsgChannelOpenConfirm(
  port_id=host_port,
  channel_id=channel_id_on_osmo,
  proof_ack=channel_proof.SerializeToString(),
  proof_height=Height(int(inj_client_trusted_revision_number), int(inj_client_trusted_height)),
  signer=osmo_wallet.key.acc_address,
)

channel_open_confirm_result = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenConfirm", msg, osmo_wallet, osmo)
channel_open_confirm_on_osmo_df = pd.DataFrame(channel_open_confirm_result["tx_response"]["logs"][0]["events"][0]["attributes"])

print(channel_open_confirm_on_osmo_df)

###################################################
#initial packet relay for ica contracts
###################################################

#relay packet
time.sleep(10) #wait a few blocks
update_client_msg = fabricate_update_client(inj, inj_rpc_url, inj_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

inj_client_trusted_height = header_height
inj_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"commitments/ports/{controller_port}/channels/{channel_id_on_inj}/sequences/{packet_to_relay.sequence}", "ascii").hex(),
    "prove": "true",
    "height": int(inj_client_trusted_height) - 1,
}
resp = requests.get(f"{inj_rpc_url}/abci_query", headers=inj_rpc_header, params=params).json()
proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
packet_proof = MerkleProof(proofs=proofs)

msg = MsgRecvPacket(
  packet=packet_to_relay,
  proof_commitment=packet_proof.SerializeToString(),
  proof_height=Height(int(inj_client_trusted_revision_number), int(inj_client_trusted_height)),
  signer=osmo_wallet.key.acc_address
)

relay_result = stargate_msg("/ibc.core.channel.v1.MsgRecvPacket", msg, osmo_wallet, osmo)
relay_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_result["tx_response"]["logs"][0]["events"]] for y in x])

print(relay_result_df)

#parse the acknowledge packet (message) for the ack relay back to the controller smart contract
ack_df = pd.DataFrame([x for x in relay_result["tx_response"]["logs"][0]["events"] if x["type"]=="write_acknowledgement"][0]["attributes"])
ack_df["index"] = 0
ack_df = ack_df.pivot(index="index", columns="key", values="value")
ack_row = ack_df.iloc[0,:]

print(ack_row)

#relay ack
time.sleep(10) #wait a few blocks
update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, inj_rpc_header, inj, inj_wallet, client_id_on_inj)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, inj_wallet, inj)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

osmo_client_trusted_height = header_height
osmo_client_trusted_revision_number = inj.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_inj}")["client_state"]["latest_height"]["revision_number"]

##fetch packet ack proofs - acks/ports/{port_id}/channels/{channel_id}/sequences/{ack_sequence}
params = {
  "path": '"/store/ibc/key"',
  "data": "0x" + bytes(f"acks/ports/{ack_row['packet_dst_port']}/channels/{ack_row['packet_dst_channel']}/sequences/{ack_row['packet_sequence']}", "ascii").hex(),
  "prove": "true",
  "height": int(osmo_client_trusted_height) - 1,
}
resp = requests.get(f"{osmo_rpc_url }/abci_query", headers=inj_rpc_header, params=params).json()
proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
ack_proof = MerkleProof(proofs=proofs)

print(ack_proof)

msg = MsgAcknowledgement(
  packet=packet_to_relay,
  acknowledgement=hexstring_to_bytes(ack_row["packet_ack_hex"]),
  proof_acked=ack_proof.SerializeToString(),
  proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
  signer=wallet.key.acc_address,
)

relay_ack_result = stargate_msg("/ibc.core.channel.v1.MsgAcknowledgement", msg, inj_wallet, inj)
relay_ack_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_ack_result["tx_response"]["logs"][0]["events"]] for y in x])

print(relay_ack_result_df)

###################################################
#persist channel info
###################################################

context["channel_id_on_inj"] = channel_id_on_inj
context["channel_id_on_osmo"] = channel_id_on_osmo
context["port_id_on_inj"] = controller_port
context["port_id_on_osmo"] = host_port
context["controller_address"] = controller_address
context["host_address"] = host_address

print(context)

with open("context.json", "w") as f:
    # Write the dictionary to the file as a JSON string
    json.dump(context, f)