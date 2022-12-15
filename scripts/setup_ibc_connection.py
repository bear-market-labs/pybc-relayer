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
sys.path.append(os.path.join(os.path.dirname(__name__), 'scripts'))

from helpers import proto_to_binary, timestamp_string_to_proto, stargate_msg, create_ibc_client, fetch_chain_objects, bech32_to_hexstring, hexstring_to_bytes, bech32_to_b64, b64_to_bytes, fabricate_update_client, fetch_proofs

#setup lcd clients, rpc urls, wallets
(terra, wallet, terra_rpc_url, terra_rpc_header) = fetch_chain_objects("pisco-1")
(osmo, osmo_wallet, osmo_rpc_url, osmo_rpc_header) = fetch_chain_objects("osmo-test-4")

############################################
#create ibc clients
############################################

result = create_ibc_client(terra, osmo, osmo_wallet, 1)
result_df = pd.DataFrame(result["tx_response"]["logs"][0]["events"][0]["attributes"])
client_id_on_osmo = result_df[result_df["key"]=="client_id"]["value"].values[0]

print(f"""

client_id on osmo: {client_id_on_osmo}

""")

result = create_ibc_client(osmo, terra, wallet, 4)
result_df = pd.DataFrame(result["tx_response"]["logs"][0]["events"][0]["attributes"])
client_id_on_terra = result_df[result_df["key"]=="client_id"]["value"].values[0]

print(f"""

client_id on terra: {client_id_on_terra}

""")

############################################
#create ibc connection
############################################

#ConnectionOpenInit
msg = MsgConnectionOpenInit(
  client_id=client_id_on_terra,
  counterparty=ConnectionCounterParty(
    client_id=client_id_on_osmo,
    prefix=MerklePrefix(
      key_prefix=bytes("ibc", "ascii"),
    )
  ),
  version=Version(
    identifier="1",
    features=["ORDER_ORDERED", "ORDER_UNORDERED"],
  ),
  delay_period=0,
  signer=wallet.key.acc_address
)

time.sleep(5) #wait a block
result = stargate_msg("/ibc.core.connection.v1.MsgConnectionOpenInit", msg, wallet, terra)
result_df = pd.DataFrame(result["tx_response"]["logs"][0]["events"][0]["attributes"])
connection_id_on_terra = result_df[result_df["key"]=="connection_id"]["value"].values[0]

print(f"connection_id on terra: {connection_id_on_terra}")

#ConnectionOpenTry
time.sleep(10) #wait a few blocks
msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_on_osmo_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", msg, osmo_wallet, osmo)
header_height = Header.FromString(msg.header.value).signed_header.header.height

terra_client_trusted_height = header_height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

client_proof, connection_proof, consensus_proof, consensus_height, client_state = fetch_proofs(terra_rpc_url, terra_rpc_header, client_id_on_terra, terra_client_trusted_height, terra_client_trusted_revision_number, connection_id_on_terra)

msg = MsgConnectionOpenTry(
  client_id=client_id_on_osmo, #str
  client_state=client_state, #Any
  counterparty=ConnectionCounterParty(
    client_id=client_id_on_terra,
    prefix=MerklePrefix(
      key_prefix=bytes("ibc", "ascii"),
    ),
    connection_id=connection_id_on_terra,
  ),
  delay_period=0, #int
  counterparty_versions=[
        Version(
          identifier="1",
          features=["ORDER_ORDERED", "ORDER_UNORDERED"],
        )
    ], #list of Version
  proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_height)), #Height
  proof_init=connection_proof.SerializeToString(), #bytes
  proof_client=client_proof.SerializeToString(), #bytes
  proof_consensus=consensus_proof.SerializeToString(), #bytes
  consensus_height=consensus_height, #Height
  signer=osmo_wallet.key.acc_address, #str
)

connection_try_on_osmo_result = stargate_msg("/ibc.core.connection.v1.MsgConnectionOpenTry", msg, osmo_wallet, osmo)
create_connection_on_osmo_df = pd.DataFrame(connection_try_on_osmo_result["tx_response"]["logs"][0]["events"][0]["attributes"])
connection_id_on_osmo = create_connection_on_osmo_df[create_connection_on_osmo_df["key"]=="connection_id"]["value"].values[0]

print(create_connection_on_osmo_df)

#ConnectionOpenAck
time.sleep(10) #wait a few blocks
msg = fabricate_update_client(osmo, osmo_rpc_url, osmo_rpc_header, terra, wallet, client_id_on_terra)
update_client_on_terra_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", msg, wallet, terra)
header_height = Header.FromString(msg.header.value).signed_header.header.height

osmo_client_trusted_height = header_height
osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_terra}")["client_state"]["latest_height"]["revision_number"]

client_proof, connection_proof, consensus_proof, consensus_height, client_state = fetch_proofs(osmo_rpc_url, osmo_rpc_header, client_id_on_osmo, osmo_client_trusted_height, osmo_client_trusted_revision_number, connection_id_on_osmo)

msg = MsgConnectionOpenAck(
  connection_id=connection_id_on_terra,
  counterparty_connection_id=connection_id_on_osmo,
  version=Version(
      identifier="1",
      features=["ORDER_ORDERED", "ORDER_UNORDERED"],
    ),
  client_state=client_state,
  proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
  proof_try=connection_proof.SerializeToString(),
  proof_client=client_proof.SerializeToString(),
  proof_consensus=consensus_proof.SerializeToString(),
  consensus_height=consensus_height,
  signer=wallet.key.acc_address,
)

connection_ack_on_terra_result = stargate_msg("/ibc.core.connection.v1.MsgConnectionOpenAck", msg, wallet, terra)
connection_ack_on_terra_df = pd.DataFrame(connection_ack_on_terra_result["tx_response"]["logs"][0]["events"][0]["attributes"])

print(connection_ack_on_terra_df)

#ConnectionOpenConfirm
time.sleep(10) #wait a few blocks
msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_on_osmo_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", msg, osmo_wallet, osmo)
header_height = Header.FromString(msg.header.value).signed_header.header.height

terra_client_trusted_height = header_height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

client_proof, connection_proof, consensus_proof, consensus_height, client_state = fetch_proofs(terra_rpc_url, terra_rpc_header, client_id_on_terra, terra_client_trusted_height, terra_client_trusted_revision_number, connection_id_on_terra)

msg = MsgConnectionOpenConfirm(
  connection_id=connection_id_on_osmo,
  proof_ack=connection_proof.SerializeToString(),
  proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_height)),
  signer=osmo_wallet.key.acc_address,
)

connection_confirm_on_osmo_result = stargate_msg("/ibc.core.connection.v1.MsgConnectionOpenConfirm", msg, osmo_wallet, osmo)
connection_confirm_on_osmo_df = pd.DataFrame(connection_confirm_on_osmo_result["tx_response"]["logs"][0]["events"][0]["attributes"])

print(connection_confirm_on_osmo_df)

############################################
#persist ibc connection details
############################################

context = {}
context["connection_id_on_terra"] = connection_id_on_terra
context["connection_id_on_osmo"] = connection_id_on_osmo
context["client_id_on_terra"] = client_id_on_terra
context["client_id_on_osmo"] = client_id_on_osmo
print(context)

with open("context.json", "w") as f:
    # Write the dictionary to the file as a JSON string
    json.dump(context, f)