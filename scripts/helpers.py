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

###############################################
# wasm tx helpers
###############################################

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

###############################################
# data formatting helpers
###############################################

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

###############################################
# lcd, rpc, wallet helpers
###############################################

class BaseAsyncAPI2(BaseAsyncAPI):
    async def query(self, query_string: str, params=None):
        if params is None:
          res = await self._c._get(query_string)
        else:
          res = await self._c._get(query_string, params=params)
        return res

    #for dispatching protobuf classes to the chain
    async def broadcast(self, tx):
        res = await self._c._post("/cosmos/tx/v1beta1/txs", {"tx_bytes": proto_to_binary(tx), "mode": "BROADCAST_MODE_BLOCK"})
        return res


class BaseAPI2(BaseAsyncAPI2):
    @sync_bind(BaseAsyncAPI2.query)
    def query(self, query_string: str):
        pass

    @sync_bind(BaseAsyncAPI2.broadcast)
    def broadcast(self, tx: Tx):
        pass

class OsmoKey(MnemonicKey):
  @property
  def acc_address(self) -> AccAddress: 
    if not self.raw_address:
      raise ValueError("could not compute acc_address: missing raw_address")
    return AccAddress(get_bech("osmo", self.raw_address.hex()))

class InjKey(MnemonicKey):
  @property
  def acc_address(self) -> AccAddress: 
    if not self.raw_address:
      raise ValueError("could not compute acc_address: missing raw_address")
    return AccAddress(get_bech("inj", self.raw_address.hex()))

def fetch_chain_objects(chain_id):

  creds = {}
  # Open the file for reading
  with open("/repos/pybc-relayer/scripts/creds.json", "r") as f:
    # Load the dictionary from the file
    creds = json.load(f)

  seed_phrase = creds["seed_phrase"]

  if chain_id == "pisco-1":
    terra = LCDClient(url="https://pisco-lcd.terra.dev/", chain_id="pisco-1")
    terra.broadcaster = BaseAPI2(terra)

    terra_rpc_url = f"https://rpc.pisco.terra.setten.io/{creds['setten_project_id']}"
    terra_rpc_header = {"Authorization": f"Bearer {creds['setten_key']}"}
    
    wallet = terra.wallet(MnemonicKey(mnemonic=seed_phrase))

    return (terra, wallet, terra_rpc_url, terra_rpc_header)
  elif chain_id == "osmo-test-4":
    osmo = LCDClient(url="https://lcd-test.osmosis.zone", chain_id="localterra")
    osmo.chain_id = "osmo-test-4"
    osmo.broadcaster = BaseAPI2(osmo)

    osmo_rpc_url = "https://rpc-test.osmosis.zone"
    osmo_rpc_header = {}

    wallet = osmo.wallet(OsmoKey(mnemonic=seed_phrase, coin_type=118))
    return (osmo, wallet, osmo_rpc_url, osmo_rpc_header)
  elif chain_id == "injective-888":
    inj = LCDClient(url="https://k8s.testnet.lcd.injective.network:443", chain_id="localterra")
    inj.chain_id = "injective-888"
    inj.broadcaster = BaseAPI2(inj)

    inj_rpc_url = "https://k8s.testnet.tm.injective.network:443/"
    inj_rpc_header = {}

    wallet = inj.wallet(InjKey(mnemonic=seed_phrase, coin_type=60))
    return (inj, wallet, inj_rpc_url, inj_rpc_header)
  
  return (None, None, None, None)
    
###############################################
# ibc helpers
###############################################

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

def fabricate_update_client(remote_lcd, remote_rpc_url, remote_rpc_header, client_lcd, client_wallet, client_id):

  print(locals())
  print("\n\n")

  tendermint_info_on_other_chain = remote_lcd.tendermint.block_info()

  timestamp = googTimestamp()
  timestamp.FromJsonString(tendermint_info_on_other_chain["block"]["header"]["time"])
  print(f"timestamp: {timestamp} \n\n")

  print("source current tendermint:")
  print(tendermint_info_on_other_chain)
  print("\n\n")

  validator_info_on_other_chain = remote_lcd.tendermint.validator_set(height=int(tendermint_info_on_other_chain["block"]["header"]["height"]))
  commit_info_on_other_chain = requests.get(f"{remote_rpc_url}/commit", headers=remote_rpc_header, params={"height": tendermint_info_on_other_chain["block"]["header"]["height"]}).json()

  print("\n\ncommit_info:\n")
  print(commit_info_on_other_chain)
  print("\n\n")

  block_proposer_on_other_chain = requests.get(f"{remote_rpc_url}/blockchain", headers=remote_rpc_header, params={"minHeight": tendermint_info_on_other_chain["block"]["header"]["height"], "maxHeight": tendermint_info_on_other_chain["block"]["header"]["height"]}).json()

  block_proposer_on_other_chain = block_proposer_on_other_chain["result"]["block_metas"][0]["header"]["proposer_address"]

  proposer_info = None

  for x in validator_info_on_other_chain["validators"]:
    if block_proposer_on_other_chain == bech32_to_hexstring(x["address"]).upper():
      proposer_info = x

  client_state_of_other_chain_on_my_chain = client_lcd.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id}")
  validator_info_of_other_chain_on_my_chain = remote_lcd.tendermint.validator_set(height=int(client_state_of_other_chain_on_my_chain["client_state"]["latest_height"]["revision_height"])+1)
  block_proposer_of_other_chain_on_my_chain = requests.get(f"{remote_rpc_url}/blockchain", headers=remote_rpc_header, params={"minHeight": int(client_state_of_other_chain_on_my_chain["client_state"]["latest_height"]["revision_height"])+1, "maxHeight": int(client_state_of_other_chain_on_my_chain["client_state"]["latest_height"]["revision_height"])+1}).json()["result"]["block_metas"][0]["header"]["proposer_address"]


  trusted_proposer_info = None

  for x in validator_info_of_other_chain_on_my_chain["validators"]:
    if block_proposer_of_other_chain_on_my_chain == bech32_to_hexstring(x["address"]).upper():
      trusted_proposer_info = x

  version_app = 0 if "app" not in commit_info_on_other_chain["result"]["signed_header"]["header"]["version"].keys() else int(commit_info_on_other_chain["result"]["signed_header"]["header"]["version"]["app"])

  return MsgUpdateClient(
    signer=client_wallet.key.acc_address,
    client_id=client_id,
    header=Any(
      type_url="/ibc.lightclients.tendermint.v1.Header",
      value=Header(
        signed_header=SignedHeader(
          header=tendermintHeader(
            version=Consensus(
              block=int(commit_info_on_other_chain["result"]["signed_header"]["header"]["version"]["block"]),
              app=version_app,
            ),
            chain_id=remote_lcd.chain_id,
            height=int(commit_info_on_other_chain["result"]["signed_header"]["header"]["height"]),
            time=Timestamp(timestamp.seconds, timestamp.nanos),
            last_block_id=BlockId(
              hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["last_block_id"]["hash"]),
              part_set_header=PartSetHeader(
                total=int(commit_info_on_other_chain["result"]["signed_header"]["header"]["last_block_id"]["parts"]["total"]),
                hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["last_block_id"]["parts"]["hash"]),
              ),
            ),
            last_commit_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["last_commit_hash"]),
            data_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["data_hash"]),
            validators_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["validators_hash"]),
            next_validators_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["next_validators_hash"]),
            consensus_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["consensus_hash"]),
            app_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["app_hash"]),
            last_results_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["last_results_hash"]),
            evidence_hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["evidence_hash"]),
            proposer_address=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["header"]["proposer_address"]),
          ),
          commit=Commit(
            height=int(commit_info_on_other_chain["result"]["signed_header"]["commit"]["height"]),
            round=int(commit_info_on_other_chain["result"]["signed_header"]["commit"]["round"]),
            block_id=BlockId(
              hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["commit"]["block_id"]["hash"]),
              part_set_header=PartSetHeader(
                total=int(commit_info_on_other_chain["result"]["signed_header"]["commit"]["block_id"]["parts"]["total"]),
                hash=hexstring_to_bytes(commit_info_on_other_chain["result"]["signed_header"]["commit"]["block_id"]["parts"]["hash"]),
              )
            ),
            signatures=[
              CommitSig(
                block_id_flag=BlockIdFlag(x["block_id_flag"]),
                validator_address=hexstring_to_bytes(x["validator_address"]),
                timestamp=timestamp_string_to_proto(x["timestamp"]),
                signature=base64.b64decode(x["signature"]) if x["signature"] is not None else None,
              )
              for x in commit_info_on_other_chain["result"]["signed_header"]["commit"]["signatures"]
            ],
          ),
        ),
        validator_set=ValidatorSet(
          validators=[
            Validator(
              address=base64.b64decode(bech32_to_b64(x["address"])),
              pub_key=PublicKey(
                ed25519=base64.b64decode(x["pub_key"]["key"])
              ) if "ed25519" in x["pub_key"]["@type"] else PublicKey(
                secp256_k1=base64.b64decode(x["pub_key"]["key"])
              ),
              voting_power=int(x["voting_power"]),
            ) for x in validator_info_on_other_chain["validators"]
          ],
          proposer=Validator(
            address=base64.b64decode(bech32_to_b64(proposer_info["address"])),
            pub_key=PublicKey(
              ed25519=base64.b64decode(proposer_info["pub_key"]["key"])
            ) if "ed25519" in proposer_info["pub_key"]["@type"] else PublicKey(
                secp256_k1=base64.b64decode(proposer_info["pub_key"]["key"])
            ),
            voting_power=int(proposer_info["voting_power"]),
          ) 
          ,
          total_voting_power=sum([int(x["voting_power"]) for x in validator_info_on_other_chain["validators"]]),
        ),
        trusted_height=Height(
          revision_number=int(client_state_of_other_chain_on_my_chain["client_state"]["latest_height"]["revision_number"]),
          revision_height=int(client_state_of_other_chain_on_my_chain["client_state"]["latest_height"]["revision_height"]),
        ),
        trusted_validators=ValidatorSet(
          validators=[
            Validator(
              address=base64.b64decode(bech32_to_b64(x["address"])),
              pub_key=PublicKey(
                ed25519=base64.b64decode(x["pub_key"]["key"])
              ) if "ed25519" in x["pub_key"]["@type"] else PublicKey(
                secp256_k1=base64.b64decode(x["pub_key"]["key"])
              ),
              voting_power=int(x["voting_power"]),
            ) for x in validator_info_of_other_chain_on_my_chain["validators"]
          ],
          proposer=Validator(
            address=base64.b64decode(bech32_to_b64(trusted_proposer_info["address"])),
            pub_key=PublicKey(
              ed25519=base64.b64decode(trusted_proposer_info["pub_key"]["key"])
            ) if "ed25519" in trusted_proposer_info["pub_key"]["@type"] else PublicKey(
                secp256_k1=base64.b64decode(trusted_proposer_info["pub_key"]["key"])
            ),
            voting_power=int(trusted_proposer_info["voting_power"]),
          ) 
          ,
          total_voting_power=sum([int(x["voting_power"]) for x in validator_info_of_other_chain_on_my_chain["validators"]]),
        ),
      ).SerializeToString(),
    ),
  )


def fetch_proofs(rpc_url, rpc_header, client_id, trusted_height, trusted_revision_number, connection_id):

  #client state proof
  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"clients/{client_id}/clientState", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  resp = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
  client_proof = MerkleProof(proofs=proofs)

  client_state = Any(
    type_url="/ibc.lightclients.tendermint.v1.ClientState",
    value=ClientState.FromString(
      Any.FromString(
        b64_to_bytes(resp["result"]["response"]["value"])
      ).value
    ).SerializeToString()
  )
  #terra rpc weirdly ignores params for same abci query path w/o sufficient sleep
  time.sleep(2)

  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"connections/{connection_id}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  resp = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  connection_proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
  connection_proof = MerkleProof(proofs=connection_proofs)

  time.sleep(3)
  params = {
    "path": '"/ibc.core.client.v1.Query/ClientState"',
    "data": "0x" + QueryClientStateRequest(client_id).SerializeToString().hex(),
    "prove": "false",
  }
  consensus_height = ClientState.FromString(QueryClientStateResponse.FromString(b64_to_bytes(requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()["result"]["response"]["value"])).client_state.value).latest_height

  time.sleep(3)
  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"clients/{client_id}/consensusStates/{consensus_height.revision_number}-{consensus_height.revision_height}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  resp = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  consensus_proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
  consensus_proof = MerkleProof(proofs=consensus_proofs)

  return (client_proof, connection_proof, consensus_proof, consensus_height, client_state)


def fetch_channel_proof(rpc_url, rpc_header, port_id, channel_id, trusted_height, trusted_revision_number):

  #client state proof
  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"channelEnds/ports/{port_id}/channels/{channel_id}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  resp = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
  channel_proof = MerkleProof(proofs=proofs)

  return channel_proof

def fetch_packet_proof(rpc_url, rpc_header, trusted_height, trusted_revision_number, packet_row, port_id, channel_id):

  time.sleep(2)

  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"commitments/ports/{port_id}/channels/{channel_id}/sequences/{packet_row['packet_sequence'][0]}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  resp = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
  packet_proof = MerkleProof(proofs=proofs)

  return packet_proof

def fetch_pending_packets(min_height, max_height, connection_id_on_packet_sender_chain, connection_id_on_packet_receiver_chain, port_id_on_sender_chain, port_id_on_receiver_chain, channel_id_on_sender_chain, channel_id_on_receiver_chain, rpc_url_for_sender_chain, rpc_header_for_sender_chain, rpc_url_for_receiver_chain, rpc_header_for_receiver_chain):

  params = {
    "query": "0x" + bytes(f"send_packet.packet_connection='{connection_id_on_packet_sender_chain}' and tx.height>={min_height} and tx.height<={max_height}", "ascii").hex(),
  }
  tx_results = requests.get(f"{rpc_url_for_sender_chain}/tx_search", headers=rpc_header_for_sender_chain, params=params).json()
  parsed_packets = [ (i, b64_to_bytes(z["key"]).decode("utf-8"), b64_to_bytes(z["value"]).decode("utf-8"))
    for (i, y) in enumerate(tx_results["result"]["txs"])
    for x in y["tx_result"]["events"] if x["type"]=="send_packet"
    for z in x["attributes"]
  ]

  pending_packets_df = pd.DataFrame()

  if len(parsed_packets) > 0:
    packets_df = pd.DataFrame(parsed_packets)
    packets_df.columns = ["index", "cols", "vals"]
    packets_df = packets_df.pivot(index="index", columns="cols", values="vals")

    #check if receiver_chain has received any of the packets
    params = {
      "path": '"/ibc.core.channel.v1.Query/UnreceivedPackets"',
      "data": "0x" + QueryUnreceivedPacketsRequest(port_id_on_receiver_chain, channel_id_on_receiver_chain, [int(x) for x in packets_df["packet_sequence"].values]).SerializeToString().hex(),
      "prove": "false",
    }
    unreceived_packets_sequence_numbers = QueryUnreceivedPacketsResponse.FromString(b64_to_bytes(requests.get(f"{rpc_url_for_receiver_chain}/abci_query", headers=rpc_header_for_receiver_chain, params=params).json()["result"]["response"]["value"])).sequences

    unreceived_packets = packets_df[packets_df["packet_sequence"].isin([str(x) for x in unreceived_packets_sequence_numbers])]

    #check if sender_chain has a packet commitment for the not-yet-received packets
    params = [(x, {
      "path": '"/ibc.core.channel.v1.Query/PacketCommitment"',
      "data": "0x" + QueryPacketCommitmentRequest(port_id_on_sender_chain, channel_id_on_sender_chain, int(x)).SerializeToString().hex(),
      "prove": "false",
    }) for x in unreceived_packets["packet_sequence"].values]

    unreceived_and_commitment = []

    for x in params:
      time.sleep(2)
      _resp = requests.get(f"{rpc_url_for_sender_chain}/abci_query", headers=rpc_header_for_sender_chain, params=x[1]).json()["result"]["response"]["value"]
      if _resp is not None:
        unreceived_and_commitment.append((x[0], QueryPacketCommitmentResponse.FromString(b64_to_bytes(_resp))))

    pending_packets_df = unreceived_packets[unreceived_packets["packet_sequence"].isin([x[0] for x in unreceived_and_commitment])]
    pending_packets_df["timed_out"] = pending_packets_df["packet_timeout_timestamp"] < str(time.time_ns())

  return pending_packets_df

def fetch_ack_proof(rpc_url, rpc_header, ack_row, trusted_height, trusted_revision_number):
  time.sleep(2)

  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"acks/ports/{ack_row['packet_dst_port']}/channels/{ack_row['packet_dst_channel']}/sequences/{ack_row['packet_sequence']}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  resp = requests.get(f"{rpc_url }/abci_query", headers=rpc_header, params=params).json()
  proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in resp["result"]["response"]["proofOps"]["ops"]]
  ack_proof = MerkleProof(proofs=proofs)

  return ack_proof