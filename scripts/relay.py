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

from helpers import proto_to_binary, timestamp_string_to_proto, stargate_msg, create_ibc_client, fetch_chain_objects, bech32_to_hexstring, hexstring_to_bytes, bech32_to_b64, b64_to_bytes, fabricate_update_client, fetch_proofs, deploy_local_wasm, init_contract, execute_msg, fetch_channel_proof, fetch_pending_packets, fetch_packet_proof, fetch_ack_proof

#setup lcd clients, rpc urls, wallets
(terra, wallet, terra_rpc_url, terra_rpc_header) = fetch_chain_objects("pisco-1")
(osmo, osmo_wallet, osmo_rpc_url, osmo_rpc_header) = fetch_chain_objects("osmo-test-4")

#load ibc client & connection information from previous notebooks
context = {}
# Open the file for reading
with open("context.json", "r") as f:
    # Load the dictionary from the file
    context = json.load(f)
    
print(context)
    
client_id_on_terra = context["client_id_on_terra"]
client_id_on_osmo = context["client_id_on_osmo"]
connection_id_on_terra = context["connection_id_on_terra"]
connection_id_on_osmo = context["connection_id_on_osmo"]
channel_id_on_terra = context["channel_id_on_terra"]
channel_id_on_osmo = context["channel_id_on_osmo"]
port_id_on_terra = context["port_id_on_terra"]
port_id_on_osmo = context["port_id_on_osmo"]
last_relayed_height_on_terra = context["last_relayed_height_on_terra"] if "last_relayed_height_on_terra" in context.keys() else terra.tendermint.block_info()["block"]["header"]["height"]
last_relayed_height_on_osmo = context["last_relayed_height_on_osmo"] if "last_relayed_height_on_osmo" in context.keys() else osmo.tendermint.block_info()["block"]["header"]["height"]
last_ack_height_on_terra = context["last_ack_height_on_terra"] if "last_ack_height_on_terra" in context.keys() else terra.tendermint.block_info()["block"]["header"]["height"]
last_ack_height_on_osmo = context["last_ack_height_on_osmo"] if "last_ack_height_on_osmo" in context.keys() else osmo.tendermint.block_info()["block"]["header"]["height"]

#################################################################################
# RELAY QUEUED PACKETS FROM BOTH CHAINS
#################################################################################

#fetch queued packets on TERRA-side
min_height = last_relayed_height_on_terra
max_height = terra.tendermint.block_info()["block"]["header"]["height"]
context["last_relayed_height_on_terra"] = max_height
pending_packets_df = fetch_pending_packets(min_height, max_height, connection_id_on_terra, connection_id_on_osmo, port_id_on_terra, port_id_on_osmo, channel_id_on_terra, channel_id_on_osmo, terra_rpc_url, terra_rpc_header, osmo_rpc_url, osmo_rpc_header)

print("terra-side packets to relay:")
print(pending_packets_df)

#fetch queued packets on OSMO-side
min_height = last_relayed_height_on_osmo
max_height = osmo.tendermint.block_info()["block"]["header"]["height"]
context["last_relayed_height_on_osmo"] = max_height
osmo_pending_packets_df = fetch_pending_packets(min_height, max_height, connection_id_on_osmo, connection_id_on_terra, port_id_on_osmo, port_id_on_terra, channel_id_on_osmo, channel_id_on_terra, osmo_rpc_url, osmo_rpc_header, terra_rpc_url, terra_rpc_header)

print("\n\nosmo-side packets to relay:")
print(osmo_pending_packets_df)

#TERRA --> OSMO
#update the client on osmo, fetch packet proofs, and dispatch relay message
time.sleep(20)
update_client_msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
terra_client_trusted_height = Header.FromString(update_client_msg.header.value).signed_header.header.height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

if pending_packets_df.shape[0] > 0:
    packet_proofs = [
      (i, fetch_packet_proof(terra_rpc_url, terra_rpc_header, terra_client_trusted_height, terra_client_trusted_revision_number, x, port_id_on_terra, channel_id_on_terra))

      for (i,x) in pending_packets_df[pending_packets_df["timed_out"]==False].iterrows()
    ]

    relay_results = []
    for proof in packet_proofs:
        outgoing_packet = pending_packets_df.iloc[proof[0], :]
        packet = Packet(
          sequence=int(outgoing_packet["packet_sequence"]),
          source_port=outgoing_packet["packet_src_port"],
          source_channel=outgoing_packet["packet_src_channel"],
          destination_port=outgoing_packet["packet_dst_port"],
          destination_channel=outgoing_packet["packet_dst_channel"],
          data=hexstring_to_bytes(outgoing_packet["packet_data_hex"]),
          timeout_height=Height(int(outgoing_packet["packet_timeout_height"].split('-')[0]), int(outgoing_packet["packet_timeout_height"].split('-')[1])),
          timeout_timestamp=int(outgoing_packet["packet_timeout_timestamp"]),
        )

        #to osmo; fetch acks from these logs (look for write_acknowledgement event)
        msg = MsgRecvPacket(
          packet=packet,
          proof_commitment=proof[1].SerializeToString(),
          proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_height)),
          signer=osmo_wallet.key.acc_address
        )

        relay_result = stargate_msg("/ibc.core.channel.v1.MsgRecvPacket", msg, osmo_wallet, osmo)
        relay_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_result["tx_response"]["logs"][0]["events"]] for y in x])
        relay_results.append(relay_result_df)

    print("\n\nterra to osmo relayed packets:")
    print(relay_results)

#OSMO --> TERRA
#update the client on osmo, fetch packet proofs, and dispatch relay message
time.sleep(10)
update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, osmo_rpc_header, terra, wallet, client_id_on_terra)
update_client_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, wallet, terra)
osmo_client_trusted_height = Header.FromString(update_client_msg.header.value).signed_header.header.height
osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_terra}")["client_state"]["latest_height"]["revision_number"]

if osmo_pending_packets_df.shape[0] > 0:
    packet_proofs = [
      (i, fetch_packet_proof(osmo_rpc_url, osmo_rpc_header, osmo_client_trusted_height, osmo_client_trusted_revision_number, x, port_on_osmo, channel_id_on_osmo))

      for (i,x) in osmo_pending_packets_df[osmo_pending_packets_df["timed_out"]==False].iterrows()
    ]

    relay_results = []
    for proof in packet_proofs:
        outgoing_packet = osmo_pending_packets_df.iloc[proof[0], :]
        packet = Packet(
          sequence=int(outgoing_packet["packet_sequence"]),
          source_port=outgoing_packet["packet_src_port"],
          source_channel=outgoing_packet["packet_src_channel"],
          destination_port=outgoing_packet["packet_dst_port"],
          destination_channel=outgoing_packet["packet_dst_channel"],
          data=hexstring_to_bytes(outgoing_packet["packet_data_hex"]),
          timeout_height=Height(int(outgoing_packet["packet_timeout_height"].split('-')[0]), int(outgoing_packet["packet_timeout_height"].split('-')[1])),
          timeout_timestamp=int(outgoing_packet["packet_timeout_timestamp"]),
        )

        #to osmo; fetch acks from these logs (look for write_acknowledgement event)
        msg = MsgRecvPacket(
          packet=packet,
          proof_commitment=proof[1].SerializeToString(),
          proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
          signer=wallet.key.acc_address
        )

        relay_result = stargate_msg("/ibc.core.channel.v1.MsgRecvPacket", msg, wallet, terra)
        relay_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_result["tx_response"]["logs"][0]["events"]] for y in x])
        relay_results.append(relay_result_df)

    print("\n\nosmo to terra relayed packets:")
    print(relay_results)

#################################################################################
# RELAY QUEUED ACKS FROM BOTH CHAINS
#################################################################################

#OSMO --> TERRA
#fetch queued acks on OSMO, and relay back to TERRA
time.sleep(10) #wait for rpc indexers to catch up

min_height = last_ack_height_on_osmo
max_height=int(osmo.tendermint.block_info()["block"]["header"]["height"])
context["last_ack_height_on_osmo"] = max_height

params = {
  "query": "0x" + bytes(f"write_acknowledgement.packet_connection='{connection_id_on_osmo}' and tx.height>={min_height} and tx.height<={max_height}", "ascii").hex(),
}
tx_results = requests.get(f"{osmo_rpc_url}/tx_search", headers=osmo_rpc_header, params=params).json()
parsed_acks = [ (i, b64_to_bytes(z["key"]).decode("utf-8"), b64_to_bytes(z["value"]).decode("utf-8"))
  for (i, y) in enumerate(tx_results["result"]["txs"])
  for x in y["tx_result"]["events"] if x["type"]=="write_acknowledgement"
  for z in x["attributes"]
]

if len(parsed_acks) > 0:
    acks_df = pd.DataFrame(parsed_acks)
    acks_df.columns = ["index", "cols", "vals"]
    acks_df = acks_df.pivot(index="index", columns="cols", values="vals")

    params = {
      "path": '"/ibc.core.channel.v1.Query/UnreceivedAcks"',
      "data": "0x" + QueryUnreceivedAcksRequest(port_id_on_terra, channel_id_on_terra, list(acks_df["packet_sequence"].map(lambda x: int(x)).values)).SerializeToString().hex(),
      "prove": "false",
    }
    
    unreceived_ack_sequence_numbers = QueryUnreceivedAcksResponse.FromString(b64_to_bytes(requests.get(f"{terra_rpc_url}/abci_query", headers=terra_rpc_header, params=params).json()["result"]["response"]["value"])).sequences
    unreceived_acks = acks_df[acks_df["packet_sequence"].isin([str(x) for x in unreceived_ack_sequence_numbers])]

    ##update client
    update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, terra_rpc_header, terra, wallet, client_id_on_terra)
    update_client_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, wallet, terra)
    header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

    osmo_client_trusted_height = header_height
    osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_terra}")["client_state"]["latest_height"]["revision_number"]


    ##fetch packetack proofs
    ack_proofs = [
        (i, fetch_ack_proof(osmo_rpc_url, osmo_rpc_header, x, osmo_client_trusted_height, osmo_client_trusted_revision_number))

        for (i,x) in unreceived_acks.iterrows()
    ]

    relay_ack_results = []
    for proof in ack_proofs:

        ack_data = acks_df.iloc[proof[0], :]
        packet = Packet(
          sequence=int(ack_data["packet_sequence"]),
          source_port=ack_data["packet_src_port"],
          source_channel=ack_data["packet_src_channel"],
          destination_port=ack_data["packet_dst_port"],
          destination_channel=ack_data["packet_dst_channel"],
          data=hexstring_to_bytes(ack_data["packet_data_hex"]),
          timeout_height=Height(int(ack_data["packet_timeout_height"].split('-')[0]), int(ack_data["packet_timeout_height"].split('-')[1])),
          timeout_timestamp=int(ack_data["packet_timeout_timestamp"]),
        )

        ##fabricate MsgAcknowledgement
        msg = MsgAcknowledgement(
          packet=packet,
          acknowledgement=hexstring_to_bytes(ack_data["packet_ack_hex"]),
          proof_acked=proof[1].SerializeToString(),
          proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
          signer=wallet.key.acc_address,
        )

        relay_ack_result = stargate_msg("/ibc.core.channel.v1.MsgAcknowledgement", msg, wallet, terra)
        relay_ack_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_ack_result["tx_response"]["logs"][0]["events"]] for y in x])
        relay_ack_results.append(relay_ack_result_df)

    print("\n\nosmo to terra relayed acks:"
    print(relay_ack_results)

#TERRA --> OSMO
#fetch queued acks on TERRA, and relay back to OSMO
time.sleep(10) #wait for rpc indexers to catch up

min_height = last_ack_height_on_terra
max_height=int(terra.tendermint.block_info()["block"]["header"]["height"])
context["last_ack_height_on_terra"] = max_height

params = {
  "query": "0x" + bytes(f"write_acknowledgement.packet_connection='{connection_id_on_terra}' and tx.height>={min_height} and tx.height<={max_height}", "ascii").hex(),
}
tx_results = requests.get(f"{terra_rpc_url}/tx_search", headers=terra_rpc_header, params=params).json()
parsed_acks = [ (i, b64_to_bytes(z["key"]).decode("utf-8"), b64_to_bytes(z["value"]).decode("utf-8"))
  for (i, y) in enumerate(tx_results["result"]["txs"])
  for x in y["tx_result"]["events"] if x["type"]=="write_acknowledgement"
  for z in x["attributes"]
]

if len(parsed_acks) > 0:

    acks_df = pd.DataFrame(parsed_acks)
    acks_df.columns = ["index", "cols", "vals"]
    acks_df = acks_df.pivot(index="index", columns="cols", values="vals")

    params = {
      "path": '"/ibc.core.channel.v1.Query/UnreceivedAcks"',
      "data": "0x" + QueryUnreceivedAcksRequest(port_on_osmo, channel_id_on_osmo, list(acks_df["packet_sequence"].map(lambda x: int(x)).values)).SerializeToString().hex(),
      "prove": "false",
    }
    unreceived_acks = QueryUnreceivedAcksResponse.FromString(b64_to_bytes(requests.get(f"{osmo_rpc_url}/abci_query", headers=osmo_rpc_header, params=params).json()["result"]["response"]["value"])).sequences


    ##update client
    update_client_msg = fabricate_update_client(terra, terra_rpc_url, osmo_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
    update_client_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
    header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

    terra_client_trusted_height = header_height
    terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]


    ##fetch packet ack proofs 
    ack_proofs = [
        fetch_ack_proof(terra_rpc_url, terra_rpc_header, x, terra_client_trusted_height, terra_client_trusted_revision_number)

        for (i,x) in acks_df.iterrows()
    ]

    relay_ack_results = []
    for proof in ack_proofs:

        ack_data = acks_df.iloc[proof[0], :]
        packet = Packet(
          sequence=int(ack_data["packet_sequence"]),
          source_port=ack_data["packet_src_port"],
          source_channel=ack_data["packet_src_channel"],
          destination_port=ack_data["packet_dst_port"],
          destination_channel=ack_data["packet_dst_channel"],
          data=hexstring_to_bytes(ack_data["packet_data_hex"]),
          timeout_height=Height(int(ack_data["packet_timeout_height"].split('-')[0]), int(ack_data["packet_timeout_height"].split('-')[1])),
          timeout_timestamp=int(ack_data["packet_timeout_timestamp"]),
        )

        ##fabricate MsgAcknowledgement
        msg = MsgAcknowledgement(
          packet=packet,
          acknowledgement=hexstring_to_bytes(ack_data["packet_ack_hex"]),
          proof_acked=proof.SerializeToString(),
          proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
          signer=osmo_wallet.key.acc_address,
        )

        relay_ack_result = stargate_msg("/ibc.core.channel.v1.MsgAcknowledgement", msg, osmo_wallet, osmo)
        relay_ack_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_ack_result["tx_response"]["logs"][0]["events"]] for y in x])
        relay_ack_results.append(relay_ack_result_df)

    print("\n\nterra to osmo relayed acks:"
    print(relay_ack_results)

#################################################################################
# HANDLE TIMED-OUT PACKETS FROM BOTH CHAINS
#################################################################################

#cleanup TERRA-side timed-out packets
timed_out_packets_df = pd.DataFrame() if pending_packets_df.shape[0] <= 0 else pending_packets_df[pending_packets_df["timed_out"]].reset_index()

if timed_out_packets_df.shape[0] > 0:

    #timeout packets
    time.sleep(10)
    update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, osmo_rpc_header, terra, wallet, client_id_on_terra)
    update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, wallet, terra)
    header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

    osmo_client_trusted_height = header_height
    osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_terra}")["client_state"]["latest_height"]["revision_number"]

    #query for the next sequence number
    params = {
      "path": '"/ibc.core.channel.v1.Query/NextSequenceReceive"',
      "data": "0x" + QueryNextSequenceReceiveRequest(port_id_on_osmo, channel_id_on_osmo).SerializeToString().hex(),
      "prove": "false",
    }
    next_sequence_number = QueryNextSequenceReceiveResponse.FromString(b64_to_bytes(requests.get(f"{osmo_rpc_url}/abci_query", headers=osmo_rpc_header, params=params).json()["result"]["response"]["value"])).next_sequence_receive


    #for each timed-out packet, fetch nonexistence proof (ie, the timed-out packet never executed on osmo), and dispatch timeout msg
    timed_out_packets_df = pending_packets_df[pending_packets_df["timed_out"]].reset_index()
    time_out_results = []
    for (i,row) in timed_out_packets_df.iterrows():

        #assemble original packet
        packet = Packet(
          sequence=int(row["packet_sequence"]),
          source_port=row["packet_src_port"],
          source_channel=row["packet_src_channel"],
          destination_port=row["packet_dst_port"],
          destination_channel=row["packet_dst_channel"],
          data=hexstring_to_bytes(row.packet_data_hex),
          timeout_height=Height(int(row["packet_timeout_height"].split('-')[0]), int(row["packet_timeout_height"].split('-')[1])),
          timeout_timestamp=int(row["packet_timeout_timestamp"]),
        )

        #prove the packet wasn't processed on osmo
        params = {
          "path": '"/store/ibc/key"',
          "data": "0x" + bytes(f"receipts/ports/{packet.destination_port}/channels/{packet.destination_channel}/sequences/{packet.sequence}", "ascii").hex(),
          "prove": "true",
          "height": int(osmo_client_trusted_height) - 1,
        }
        resp = requests.get(f"{osmo_rpc_url }/abci_query", headers=osmo_rpc_header, params=params).json()
        proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
        receipt_proof = MerkleProof(proofs=proofs)

        #attach original packet & "nonexistence proof", and dispatch time out message
        msg = MsgTimeout(
          packet=packet,
          proof_unreceived=receipt_proof.SerializeToString(),
          proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
          next_sequence_recv=next_sequence_number,
          signer=wallet.key.acc_address,
        )

        timeout_result = stargate_msg("/ibc.core.channel.v1.MsgTimeout", msg, wallet, terra)
        timeout_result_df = pd.DataFrame([y for x in [x["attributes"] for x in timeout_result["tx_response"]["logs"][0]["events"]] for y in x])
        time_out_results.append(timeout_result_df)

    print("\n\nterra-side timed-out packets:"
    print(time_out_results)

#cleanup OSMO-side timed-out packets
timed_out_packets_df = pd.DataFrame() if osmo_pending_packets_df.shape[0] <= 0 else osmo_pending_packets_df[osmo_pending_packets_df["timed_out"]].reset_index()

if timed_out_packets_df.shape[0] > 0:

    #timeout packets
    time.sleep(10)
    update_client_msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
    update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
    header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

    terra_client_trusted_height = header_height
    terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

    #query for the next sequence number
    params = {
      "path": '"/ibc.core.channel.v1.Query/NextSequenceReceive"',
      "data": "0x" + QueryNextSequenceReceiveRequest(port_id_on_terra, channel_id_on_terra).SerializeToString().hex(),
      "prove": "false",
    }
    next_sequence_number = QueryNextSequenceReceiveResponse.FromString(b64_to_bytes(requests.get(f"{terra_rpc_url}/abci_query", headers=terra_rpc_header, params=params).json()["result"]["response"]["value"])).next_sequence_receive


    #for each timed-out packet, fetch nonexistence proof (ie, the timed-out packet never executed on osmo), and dispatch timeout msg
    timed_out_packets_df = osmo_pending_packets_df[osmo_pending_packets_df["timed_out"]].reset_index()
    time_out_results = []
    for (i,row) in timed_out_packets_df.iterrows():

        packet = Packet(
          sequence=int(row["packet_sequence"]),
          source_port=row["packet_src_port"],
          source_channel=row["packet_src_channel"],
          destination_port=row["packet_dst_port"],
          destination_channel=row["packet_dst_channel"],
          data=hexstring_to_bytes(row.packet_data_hex),
          timeout_height=Height(int(row["packet_timeout_height"].split('-')[0]), int(row["packet_timeout_height"].split('-')[1])),
          timeout_timestamp=int(row["packet_timeout_timestamp"]),
        )

        params = {
          "path": '"/store/ibc/key"',
          "data": "0x" + bytes(f"receipts/ports/{packet.destination_port}/channels/{packet.destination_channel}/sequences/{packet.sequence}", "ascii").hex(),
          "prove": "true",
          "height": int(terra_client_trusted_height) - 1,
        }
        resp = requests.get(f"{terra_rpc_url }/abci_query", headers=terra_rpc_header, params=params).json()
        proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
        receipt_proof = MerkleProof(proofs=proofs)

        msg = MsgTimeout(
          packet=packet,
          proof_unreceived=receipt_proof.SerializeToString(),
          proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_revision_number)),
          next_sequence_recv=next_sequence_number,
          signer=osmo_wallet.key.acc_address,
        )

        timeout_result = stargate_msg("/ibc.core.channel.v1.MsgTimeout", msg, osmo_wallet, osmo)
        timeout_result_df = pd.DataFrame([y for x in [x["attributes"] for x in timeout_result["tx_response"]["logs"][0]["events"]] for y in x])
        time_out_results.append(timeout_result_df)

    print("\n\nosmo-side timed-out packets:"
    print(time_out_results)

#persist context w/ updated heights
print(context)

with open("context.json", "w") as f:
    # Write the dictionary to the file as a JSON string
    json.dump(context, f)