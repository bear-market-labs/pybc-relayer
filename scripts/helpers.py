###################################################
# imports & boilerplate functions
###################################################

import pandas as pd
import json
import os
from terra_sdk.client.lcd import LCDClient
from terra_sdk.core.wasm import MsgStoreCode, MsgInstantiateContract, MsgExecuteContract
from terra_sdk.core.bank import MsgSend
from terra_sdk.core.fee import Fee
from terra_sdk.key.mnemonic import MnemonicKey
from terra_sdk.core.bech32 import get_bech
from terra_sdk.core import AccAddress, Coin, Coins
from terra_sdk.client.lcd.api.tx import CreateTxOptions, SignerOptions
from terra_sdk.client.localterra import LocalTerra
import base64
import requests
from terra_sdk.core.wasm.data import AccessConfig
from terra_proto.cosmwasm.wasm.v1 import AccessType
import subprocess
from bech32 import bech32_decode, bech32_encode, convertbits
from terra_sdk.client.lcd.api._base import BaseAsyncAPI, sync_bind
from terra_proto.cosmos.tx.v1beta1 import Tx, TxBody, AuthInfo, SignDoc, SignerInfo, ModeInfo, ModeInfoSingle, BroadcastTxResponse
from terra_proto.cosmos.base.abci.v1beta1 import TxResponse
from terra_proto.cosmos.tx.signing.v1beta1 import SignMode
from terra_proto.ibc.core.channel.v1 import MsgChannelOpenInit, Channel, State, Order, Counterparty, MsgChannelOpenTry, MsgChannelOpenAck, MsgChannelOpenConfirm, QueryUnreceivedPacketsRequest, QueryUnreceivedPacketsResponse, QueryPacketCommitmentRequest, QueryPacketCommitmentResponse, Packet, QueryNextSequenceReceiveRequest, QueryNextSequenceReceiveResponse, MsgRecvPacket, MsgTimeout, QueryUnreceivedAcksRequest, QueryUnreceivedAcksResponse, MsgAcknowledgement
from terra_proto.ibc.core.client.v1 import MsgCreateClient, Height, MsgUpdateClient, QueryClientStateRequest, QueryClientStateResponse
from terra_proto.ibc.core.connection.v1 import MsgConnectionOpenInit, Counterparty as ConnectionCounterParty, Version, MsgConnectionOpenTry, MsgConnectionOpenAck, MsgConnectionOpenConfirm
from terra_proto.ibc.lightclients.tendermint.v1 import ClientState, ConsensusState, Fraction, Header
from terra_proto.ics23 import HashOp, LengthOp, LeafOp, InnerOp, ProofSpec, InnerSpec, CommitmentProof, ExistenceProof, NonExistenceProof, BatchProof, CompressedBatchProof, BatchEntry, CompressedBatchEntry, CompressedExistenceProof, CompressedNonExistenceProof
from terra_proto.ibc.core.commitment.v1 import MerkleRoot, MerklePrefix, MerkleProof
from terra_proto.tendermint.types import ValidatorSet, Validator, SignedHeader, Header as tendermintHeader, Commit, BlockId, PartSetHeader, CommitSig, BlockIdFlag
from terra_proto.tendermint.version import Consensus
from terra_proto.tendermint.crypto import PublicKey
from betterproto.lib.google.protobuf import Any
from betterproto import Timestamp
import math
from dateutil.parser import parse
from datetime import datetime, timedelta
from ecdsa import SECP256k1, SigningKey
from ecdsa.util import sigencode_string_canonize
import hashlib
import bech32
import time

from google.protobuf.json_format import Parse, ParseDict
from google.protobuf.timestamp_pb2 import Timestamp as googTimestamp

def deploy_local_wasm(file_path, wallet, terra):

  print(f"file_path: {file_path}\nwallet: {wallet.key.acc_address}")

  fee = Fee(10000000, "2000000uosmo")
  if ("terra" in terra.chain_id) or ("pisco" in terra.chain_id):
    fee = Fee(6900000, "2000000uluna")

  with open(file_path, "rb") as fp:
    file_bytes = base64.b64encode(fp.read()).decode()
    store_code_msg = MsgStoreCode(wallet.key.acc_address, file_bytes, instantiate_permission=AccessConfig(AccessType.ACCESS_TYPE_EVERYBODY, ""))
    store_code_tx = wallet.create_and_sign_tx(CreateTxOptions(msgs=[store_code_msg], fee=fee))
    store_code_result = terra.tx.broadcast(store_code_tx)

  #persist code_id
  deployed_code_id = store_code_result.logs[0].events_by_type["store_code"]["code_id"][0]

  return deployed_code_id

def init_contract(code_id, init_msg, wallet, terra, name):

  fee = Fee(690000, "500000uosmo")
  if ("terra" in terra.chain_id) or ("pisco" in terra.chain_id):
    fee = Fee(690000, "500000uluna")

  #invoke contract instantiate
  instantiate_msg = MsgInstantiateContract(
    wallet.key.acc_address,
    wallet.key.acc_address,
    code_id,
    name,
    init_msg,
  )

  instantiate_tx = wallet.create_and_sign_tx(CreateTxOptions(msgs=[instantiate_msg], fee=fee))
  instantiate_tx_result = terra.tx.broadcast(instantiate_tx)

  return instantiate_tx_result

def execute_msg(address, msg, wallet, terra, coins=None):

  fee = Fee(4000000, "1000000uosmo")
  if ("terra" in terra.chain_id) or ("pisco" in terra.chain_id):
    fee = Fee(6900000, "1500000uluna")

  execute_msg = MsgExecuteContract(
    sender=wallet.key.acc_address,
    contract=address,
    msg=msg,
    coins=coins 
  )

  #there is a fixed UST fee component now, so it's easier to pay fee in UST
  tx = wallet.create_and_sign_tx(CreateTxOptions(msgs=[execute_msg], fee=fee))
  tx_result = terra.tx.broadcast(tx)

  return tx_result

def bank_msg_send(recipient, amount, wallet, terra):

  fee = Fee(2000000, "500000uosmo")
  if ("terra" in terra.chain_id) or ("pisco" in terra.chain_id):
    fee = Fee(6900000, "150000uluna")

  bank_msg = MsgSend(
    from_address=wallet.key.acc_address,
    to_address=recipient,
    amount=amount,
  )

  #there is a fixed UST fee component now, so it's easier to pay fee in UST
  tx = wallet.create_and_sign_tx(CreateTxOptions(msgs=[bank_msg], fee=fee))
  tx_result = terra.tx.broadcast(tx)

  return tx_result

def stargate_msg(type_url, msg, wallet, terra):

  if "888" in terra.chain_id:
    account_data = terra.broadcaster.query(f"/cosmos/auth/v1beta1/accounts/{wallet.key.acc_address}")
    account_number = int(account_data["account"]["base_account"]["account_number"])
    sequence = int(account_data["account"]["base_account"]["sequence"])
  else:
    account_number = wallet.account_number_and_sequence()["account_number"]
    sequence = wallet.account_number_and_sequence()["sequence"]

  chain_id = terra.chain_id


  if "888" in terra.chain_id:
    broadcaster = terra.broadcaster
    fee = Fee(3000000, "1500000000000000inj")
  elif not(("terra" in terra.chain_id) or ("pisco" in terra.chain_id)):
    broadcaster = terra.broadcaster
    fee = Fee(2000000, "500000uosmo")
  else:
    broadcaster = terra.broadcaster
    fee = Fee(690000, "15000uluna")

  # format msgs for tx
  tx_body = TxBody(
    messages=[
      Any(type_url=type_url, value=bytes(msg))
    ],
    memo="",
    timeout_height=0
  )

  # publish public key, create sign-document, and produce signature 
  signer_info = SignerInfo(
    public_key=wallet.key.public_key.pack_any(),
    mode_info=ModeInfo(
      single=ModeInfoSingle(
        mode=SignMode.SIGN_MODE_DIRECT
      )
    ),
    sequence=sequence,
  )

  auth_info = AuthInfo(
    signer_infos=[signer_info],
    fee=fee.to_proto(),
  )

  sign_doc = SignDoc(
    body_bytes=bytes(tx_body),
    auth_info_bytes=bytes(auth_info),
    chain_id=chain_id,
    account_number=account_number
  )

  sk = SigningKey.from_string(wallet.key.private_key, curve=SECP256k1)
  signature = sk.sign_deterministic(
    data=bytes(sign_doc),
    hashfunc=hashlib.sha256,
    sigencode=sigencode_string_canonize,
  )

  # fabricate ready-to-send tx (messages, signer public info, signatures)
  tx = Tx(
    body=tx_body,
    auth_info=auth_info,
    signatures=[signature]
  )

  # post to lcd txs endpoint
  tx_result = broadcaster.broadcast(tx)

  return tx_result


def to_binary(msg):
  return base64.b64encode(json.dumps(msg).encode("utf-8")).decode("utf-8")

def proto_to_binary(msg):
  return base64.b64encode(msg.SerializeToString()).decode("utf-8")

def b64_to_bytes(b64_string):
  return base64.b64decode(b64_string)

def b64_to_hexstring(b64_string):
  return base64.b64decode(b64_string).hex()

def hexstring_to_b64(hexstring):
  return base64.b64encode(bytes.fromhex(hexstring))

def hexstring_to_bytes(hexstring):
  return bytes.fromhex(hexstring)

def hexstring_to_bech32(prefix, hexstring):
  #bech32.convertbits maps a hex to the bech32 charset index
  return bech32.bech32_encode(prefix, bech32.convertbits(hexstring_to_bytes(hexstring), 8, 5))

def b64_to_bech32(prefix, b64_string):
  the_bytes = b64_to_bytes(b64_string)
  return bech32.bech32_encode(prefix, bech32.convertbits(the_bytes, 8, 5))

def bech32_to_b64(address):
  data = bech32.bech32_decode(address)[1]
  the_bytes = bytearray(bech32.convertbits(data, 5, 8))
  return base64.b64encode(the_bytes)

def bech32_to_hexstring(address):
  data = bech32.bech32_decode(address)[1]
  the_bytes = bytearray(bech32.convertbits(data, 5, 8))
  return the_bytes.hex()

def timestamp_string_to_proto(timestamp_string):
  timestamp = googTimestamp()
  timestamp.FromJsonString(timestamp_string)
  return Timestamp(timestamp.seconds, timestamp.nanos)


def create_ibc_client(foreign_chain_lcd, domestic_chain_lcd, domestic_chain_wallet, latest_height_revision_number=1):

  unbonding_period = int(foreign_chain_lcd.staking.parameters()["unbonding_time"].replace('s', ''))
  trusting_period = math.floor(unbonding_period * 2 / 3)
  max_clock_drift = 20
  terra_tendermint_info = foreign_chain_lcd.tendermint.block_info()["block"]

  print(f"""
  {foreign_chain_lcd.chain_id} information for client on {domestic_chain_lcd.chain_id} 

  unbonding_period: {unbonding_period}
  trusting_period: {trusting_period}
  max_clock_drift: {max_clock_drift}
  tendermint_info: {terra_tendermint_info}

  """)

  msg = MsgCreateClient(
    client_state=Any(
      type_url="/ibc.lightclients.tendermint.v1.ClientState",
      value=ClientState(
        chain_id=foreign_chain_lcd.chain_id,
        trust_level=Fraction(1,3),
        trusting_period=timedelta(seconds=trusting_period),
        unbonding_period=timedelta(seconds=unbonding_period),
        max_clock_drift=timedelta(seconds=max_clock_drift),
        frozen_height=Height(0,0),
        latest_height=Height(latest_height_revision_number, int(terra_tendermint_info["header"]["height"])),
        proof_specs=[
          ProofSpec(
            leaf_spec=LeafOp(
              hash=HashOp.SHA256,
              prehash_key=HashOp.NO_HASH,
              prehash_value=HashOp.SHA256,
              length=LengthOp.VAR_PROTO,
              prefix=base64.b64decode(b"AA=="),
            ),
            inner_spec=InnerSpec(
              child_order=[0,1],
              child_size=33,
              min_prefix_length=4,
              max_prefix_length=12,
              #empty_child=b'',
              hash=HashOp.SHA256,
            ),
            max_depth=0,
            min_depth=0
          ),
          ProofSpec(
            leaf_spec=LeafOp(
              hash=HashOp.SHA256,
              prehash_key=HashOp.NO_HASH,
              prehash_value=HashOp.SHA256,
              length=LengthOp.VAR_PROTO,
              prefix=base64.b64decode(b"AA=="),
            ),
            inner_spec=InnerSpec(
              child_order=[0,1],
              child_size=32,
              min_prefix_length=1,
              max_prefix_length=1,
              #empty_child=b'',
              hash=HashOp.SHA256,
            ),
            max_depth=0,
            min_depth=0
          ),
        ],
        upgrade_path=["upgrade", "upgradedIBCState"],
        allow_update_after_expiry=True,
        allow_update_after_misbehaviour=True,
      ).SerializeToString()
    ),
    consensus_state=Any(
      type_url="/ibc.lightclients.tendermint.v1.ConsensusState",
      value=ConsensusState(
        timestamp=timestamp_string_to_proto(terra_tendermint_info["header"]["time"]),
        root=MerkleRoot(base64.b64decode(terra_tendermint_info["header"]["app_hash"])),
        next_validators_hash=base64.b64decode(terra_tendermint_info["header"]["next_validators_hash"]),
      ).SerializeToString(),
    ),
    signer=domestic_chain_wallet.key.acc_address,
  )

  print(f"""

  ibc client creation message to be run {domestic_chain_lcd.chain_id}

  {msg.to_dict()}

  """)

  return stargate_msg("/ibc.core.client.v1.MsgCreateClient", msg, domestic_chain_wallet, domestic_chain_lcd)
