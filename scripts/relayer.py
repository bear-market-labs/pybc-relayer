###################################################
# imports & boilerplate functions
###################################################

from lib2to3.pgen2 import token
from unittest import runner
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
from terra_proto.ibc.core.client.v1 import MsgCreateClient, Height, MsgUpdateClient, QueryClientStateRequest, QueryClientStateResponse, 
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

################################################
# helper tx execute functions
################################################

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

  #there is a fixed UST fee component now, so it's easier to pay fee in UST
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

################################################
# formatting helper functions
################################################

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

#send any non-osmo/ion coin to wallet10
def cleanup_wallet(wallet, osmo):
  for c in osmo.bank.balance(wallet.key.acc_address)[0].to_list():
    if c.denom not in ["uion", "uosmo"]:
      bank_msg_send(wallet_10.key.acc_address, Coins.from_str(str(c)), wallet, osmo)


def timestamp_string_to_proto(timestamp_string):
  timestamp = googTimestamp()
  timestamp.FromJsonString(timestamp_string)
  return Timestamp(timestamp.seconds, timestamp.nanos)

###################################################
# clients & wallets
###################################################

terra = LCDClient(url="http://3.88.107.200:1317", chain_id="localterra")
terra_rpc_url = "http://3.88.107.200:26657"
terra_rpc_header = {"Authorization": "Bearer 4a68cbb2303d4c109ea99f7bf7ede000"}

terra = LCDClient(url="https://pisco-lcd.terra.dev/", chain_id="pisco-1")
#backup terra = LCDClient(url="https://pisco-api.dalnim.finance/", chain_id="pisco-1")
terra_rpc_url = "https://rpc.pisco.terra.setten.io/a0a3abea69544a99a67700ce2c7926fb"
terra_rpc_header = {"Authorization": "Bearer 4a68cbb2303d4c109ea99f7bf7ede000"}
#https://terra-rpc.polkachu.com/ backup rpc



wallet1 = terra.wallet(MnemonicKey(mnemonic="notice oak worry limit wrap speak medal online prefer cluster roof addict wrist behave treat actual wasp year salad speed social layer crew genius"))

wallet2 = terra.wallet(MnemonicKey(mnemonic="quality vacuum heart guard buzz spike sight swarm shove special gym robust assume sudden deposit grid alcohol choice devote leader tilt noodle tide penalty"))

wallet3 = terra.wallet(MnemonicKey(mnemonic='symbol force gallery make bulk round subway violin worry mixture penalty kingdom boring survey tool fringe patrol sausage hard admit remember broken alien absorb'))

wallet4 = terra.wallet(MnemonicKey(mnemonic='bounce success option birth apple portion aunt rural episode solution hockey pencil lend session cause hedgehog slender journey system canvas decorate razor catch empty'))

wallet = terra.wallet(MnemonicKey(mnemonic="differ flight humble cry abandon inherit noodle blood sister potato there denial woman sword divide funny trash empty novel odor churn grid easy pelican"))


################################################
# osmo client
################################################

class AsyncOsmosisAPI(BaseAsyncAPI):
    async def pool(self, id: int):
        res = await self._c._get(f"/osmosis/gamm/v1beta1/pools/{id}")
        return res

    async def query(self, query_string: str, params=None):
        if params is None:
          res = await self._c._get(query_string)
        else:
          res = await self._c._get(query_string, params=params)
        return res

    async def broadcast(self, tx):
        res = await self._c._post("/cosmos/tx/v1beta1/txs", {"tx_bytes": proto_to_binary(tx), "mode": "BROADCAST_MODE_BLOCK"})
        return res


class OsmosisAPI(AsyncOsmosisAPI):
    @sync_bind(AsyncOsmosisAPI.pool)
    def pool(self, id: int):
        pass

    @sync_bind(AsyncOsmosisAPI.query)
    # see https://lcd-test.osmosis.zone/swagger/#/
    def query(self, query_string: str):
        pass

    @sync_bind(AsyncOsmosisAPI.broadcast)
    def broadcast(self, tx: Tx):
        pass

#chain_id only seems to be enforced on init
osmo = LCDClient(url="http://54.92.222.57:1317", chain_id="localterra")
osmo_rpc_url = "http://54.92.222.57:26657"
osmo.chain_id = "localosmosis"

osmo = LCDClient(url="https://lcd-test.osmosis.zone", chain_id="localterra")
osmo.chain_id = "osmo-test-4"
osmo_rpc_url = "https://rpc-test.osmosis.zone"

osmo.broadcaster = OsmosisAPI(osmo)

terra.broadcaster = OsmosisAPI(terra)
################################################
# wallets
################################################

#override terra prefix
class OsmoKey(MnemonicKey):
  @property
  def acc_address(self) -> AccAddress: 
    if not self.raw_address:
      raise ValueError("could not compute acc_address: missing raw_address")
    return AccAddress(get_bech("osmo", self.raw_address.hex()))

osmo_wallet1 = osmo.wallet(OsmoKey(mnemonic="notice oak worry limit wrap speak medal online prefer cluster roof addict wrist behave treat actual wasp year salad speed social layer crew genius", coin_type=118))

osmo_wallet2 = osmo.wallet(OsmoKey(mnemonic="quality vacuum heart guard buzz spike sight swarm shove special gym robust assume sudden deposit grid alcohol choice devote leader tilt noodle tide penalty", coin_type=118))

osmo_wallet3 = osmo.wallet(OsmoKey(mnemonic='symbol force gallery make bulk round subway violin worry mixture penalty kingdom boring survey tool fringe patrol sausage hard admit remember broken alien absorb', coin_type=118))

osmo_wallet4 = osmo.wallet(OsmoKey(mnemonic='bounce success option birth apple portion aunt rural episode solution hockey pencil lend session cause hedgehog slender journey system canvas decorate razor catch empty', coin_type=118))

osmo_wallet_10 = osmo.wallet(OsmoKey(mnemonic="prefer forget visit mistake mixture feel eyebrow autumn shop pair address airport diesel street pass vague innocent poem method awful require hurry unhappy shoulder", coin_type=118))

osmo_wallet = osmo.wallet(OsmoKey(mnemonic="differ flight humble cry abandon inherit noodle blood sister potato there denial woman sword divide funny trash empty novel odor churn grid easy pelican", coin_type=118))



################################################
# inj client
################################################

class AsyncInjAPI(BaseAsyncAPI):

    async def query(self, query_string: str):
        res = await self._c._get(query_string)
        return res

    async def broadcast(self, tx):
        res = await self._c._post("/cosmos/tx/v1beta1/txs", {"tx_bytes": proto_to_binary(tx), "mode": "BROADCAST_MODE_BLOCK"})
        return res


class InjAPI(AsyncInjAPI):

    @sync_bind(AsyncInjAPI.query)
    # see https://lcd-test.osmosis.zone/swagger/#/
    def query(self, query_string: str):
        pass

    @sync_bind(AsyncInjAPI.broadcast)
    def broadcast(self, tx: Tx):
        pass

#chain_id only seems to be enforced on init
inj = LCDClient(url="https://k8s.testnet.lcd.injective.network:443", chain_id="localterra")
inj.chain_id = "injective-888"
inj.broadcaster = InjAPI(inj)
inj_rpc_url = "https://k8s.testnet.tm.injective.network:443/"

################################################
# inj wallets
################################################

#override terra prefix
class InjKey(MnemonicKey):
  @property
  def acc_address(self) -> AccAddress: 
    if not self.raw_address:
      raise ValueError("could not compute acc_address: missing raw_address")
    return AccAddress(get_bech("inj", self.raw_address.hex()))

nuhmonik = "differ flight humble cry abandon inherit noodle blood sister potato there denial woman sword divide funny trash empty novel odor churn grid easy pelican"
inj_wallet = inj.wallet(InjKey(mnemonic=nuhmonik, coin_type=60))

#################################################
#create ibc clients
#################################################

def create_ibc_client(foreign_chain_lcd, domestic_chain_lcd, domestic_chain_wallet, latest_height_revision_number=1):

  print(locals())
  print("\n\n")

  unbonding_period = int(foreign_chain_lcd.staking.parameters()["unbonding_time"].replace('s', ''))
  trusting_period = math.floor(unbonding_period * 2 / 3)
  max_clock_drift = 20
  terra_tendermint_info = foreign_chain_lcd.tendermint.block_info()["block"]

  print(terra_tendermint_info)
  print("\n\n")

  terra_client_msg = MsgCreateClient(
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

  return stargate_msg("/ibc.core.client.v1.MsgCreateClient", terra_client_msg, domestic_chain_wallet, domestic_chain_lcd)

if True:
  create_terra_ibc_client_on_osmo_result = create_ibc_client(terra, osmo, osmo_wallet, 1)
  create_terra_ibc_client_df = pd.DataFrame(create_terra_ibc_client_on_osmo_result["tx_response"]["logs"][0]["events"][0]["attributes"])
  terra_client_id = create_terra_ibc_client_df[create_terra_ibc_client_df["key"]=="client_id"]["value"].values[0]


  create_osmo_ibc_client_on_terra_result = create_ibc_client(osmo, terra, wallet, 4)
  create_osmo_ibc_client_df = pd.DataFrame(create_osmo_ibc_client_on_terra_result["tx_response"]["logs"][0]["events"][0]["attributes"])
  osmo_client_id = create_osmo_ibc_client_df[create_osmo_ibc_client_df["key"]=="client_id"]["value"].values[0]
else:
  create_terra_ibc_client_on_inj_result = create_ibc_client(terra, inj, inj_wallet, 1)
  create_terra_ibc_client_df = pd.DataFrame(create_terra_ibc_client_on_inj_result["tx_response"]["logs"][0]["events"][0]["attributes"])
  terra_client_id = create_terra_ibc_client_df[create_terra_ibc_client_df["key"]=="client_id"]["value"].values[0]

  create_inj_ibc_client_on_terra_result = create_ibc_client(inj, terra, wallet, 888)
  create_inj_ibc_client_df = pd.DataFrame(create_inj_ibc_client_on_terra_result["tx_response"]["logs"][0]["events"][0]["attributes"])
  inj_client_id = create_inj_ibc_client_df[create_inj_ibc_client_df["key"]=="client_id"]["value"].values[0]

################################################
# create ibc connection (init)
################################################

def create_connection(lcd, wallet, foreign_client_id, domestic_client_id):
  create_connection_msg = MsgConnectionOpenInit(
    client_id=foreign_client_id,
    counterparty=ConnectionCounterParty(
      client_id=domestic_client_id,
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

  return stargate_msg("/ibc.core.connection.v1.MsgConnectionOpenInit", create_connection_msg, wallet, lcd)

if True:
  create_connection_result = create_connection(terra, wallet, osmo_client_id, terra_client_id)
  create_connection_terra_osmo_client_df = pd.DataFrame(create_connection_result["tx_response"]["logs"][0]["events"][0]["attributes"])
  connection_id = create_connection_terra_osmo_client_df[create_connection_terra_osmo_client_df["key"]=="connection_id"]["value"].values[0]

else:
  #some weird rate-limiting going on
  time.sleep(1)
  create_connection_result = create_connection(terra, wallet, inj_client_id, terra_client_id)
  create_connection_terra_osmo_client_df = pd.DataFrame(create_connection_result["tx_response"]["logs"][0]["events"][0]["attributes"])
  connection_id = create_connection_terra_osmo_client_df[create_connection_terra_osmo_client_df["key"]=="connection_id"]["value"].values[0]

#start connection init ack
"""
source is the ibc client initiating the connection (terra initiating connection to injective)
this function will call update client on the *destination* client (following the example, injective)
"""
def fabricate_update_client(remote_lcd, remote_rpc_url, remote_rpc_header, client_lcd, client_wallet, client_id):

  print(locals())
  print("\n\n")

  ## update terra-client on inj

  source_current_tendermint_info = remote_lcd.tendermint.block_info()

  timestamp = googTimestamp()
  timestamp.FromJsonString(source_current_tendermint_info["block"]["header"]["time"])
  print(f"timestamp: {timestamp} \n\n")

  print("source current tendermint:")
  print(source_current_tendermint_info)
  print("\n\n")

  source_current_validator_info = remote_lcd.tendermint.validator_set(height=int(source_current_tendermint_info["block"]["header"]["height"]))
  #source_current_validator_info = requests.get(f"{remote_rpc_url}/validators", headers=remote_rpc_header, params={"height": source_current_tendermint_info["block"]["header"]["height"]}).json()
  source_current_commit_info = requests.get(f"{remote_rpc_url}/commit", headers=remote_rpc_header, params={"height": source_current_tendermint_info["block"]["header"]["height"]}).json()

  print("\n\ncommit_info:\n")
  print(source_current_commit_info)
  print("\n\n")

  source_current_block_proposer = requests.get(f"{remote_rpc_url}/blockchain", headers=remote_rpc_header, params={"minHeight": source_current_tendermint_info["block"]["header"]["height"], "maxHeight": source_current_tendermint_info["block"]["header"]["height"]}).json()
  
  #["result"]["block_metas"][0]["header"]["proposer_address"]
  print(source_current_block_proposer)

  source_current_block_proposer = source_current_block_proposer["result"]["block_metas"][0]["header"]["proposer_address"]

  proposer_info = None

  for x in source_current_validator_info["validators"]:
    if source_current_block_proposer == bech32_to_hexstring(x["address"]).upper():
      proposer_info = x

  source_client_on_dest_state = client_lcd.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id}")
  source_trusted_validator_info = remote_lcd.tendermint.validator_set(height=int(source_client_on_dest_state["client_state"]["latest_height"]["revision_height"])+1)
  source_trusted_block_proposer = requests.get(f"{remote_rpc_url}/blockchain", headers=remote_rpc_header, params={"minHeight": int(source_client_on_dest_state["client_state"]["latest_height"]["revision_height"])+1, "maxHeight": int(source_client_on_dest_state["client_state"]["latest_height"]["revision_height"])+1}).json()["result"]["block_metas"][0]["header"]["proposer_address"]


  trusted_proposer_info = None

  for x in source_trusted_validator_info["validators"]:
    """
    random fyi - a validator's public key is its sha256'd b64 bytes, and then slice'd 20

    sha = hashlib.sha256()
    sha.update(base64.b64decode(x["pub_key"]["key"]))
    if base64.b64encode(sha.digest()[0:20]).decode("utf-8") == source_trusted_tendermint_info["block"]["header"]["proposer_address"]:
      trusted_proposer_info = x
    """

    if source_trusted_block_proposer == bech32_to_hexstring(x["address"]).upper():
      trusted_proposer_info = x

  print("\n\n")
  print(source_current_commit_info["result"]["signed_header"]["commit"]["signatures"])

  version_app = 0 if "app" not in source_current_commit_info["result"]["signed_header"]["header"]["version"].keys() else int(source_current_commit_info["result"]["signed_header"]["header"]["version"]["app"])

  return MsgUpdateClient(
    signer=client_wallet.key.acc_address,
    client_id=client_id,
    header=Any(
      type_url="/ibc.lightclients.tendermint.v1.Header",
      value=Header(
        signed_header=SignedHeader(
          header=tendermintHeader(
            version=Consensus(
              block=int(source_current_commit_info["result"]["signed_header"]["header"]["version"]["block"]),
              app=version_app,
            ),
            chain_id=remote_lcd.chain_id,
            height=int(source_current_commit_info["result"]["signed_header"]["header"]["height"]),
            time=Timestamp(timestamp.seconds, timestamp.nanos),
            last_block_id=BlockId(
              hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["last_block_id"]["hash"]),
              part_set_header=PartSetHeader(
                total=int(source_current_commit_info["result"]["signed_header"]["header"]["last_block_id"]["parts"]["total"]),
                hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["last_block_id"]["parts"]["hash"]),
              ),
            ),
            last_commit_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["last_commit_hash"]),
            data_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["data_hash"]),
            validators_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["validators_hash"]),
            next_validators_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["next_validators_hash"]),
            consensus_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["consensus_hash"]),
            app_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["app_hash"]),
            last_results_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["last_results_hash"]),
            evidence_hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["evidence_hash"]),
            proposer_address=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["header"]["proposer_address"]),
          ),
          commit=Commit(
            height=int(source_current_commit_info["result"]["signed_header"]["commit"]["height"]),
            round=int(source_current_commit_info["result"]["signed_header"]["commit"]["round"]),
            block_id=BlockId(
              hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["commit"]["block_id"]["hash"]),
              part_set_header=PartSetHeader(
                total=int(source_current_commit_info["result"]["signed_header"]["commit"]["block_id"]["parts"]["total"]),
                hash=hexstring_to_bytes(source_current_commit_info["result"]["signed_header"]["commit"]["block_id"]["parts"]["hash"]),
              )
            ),
            signatures=[
              CommitSig(
                block_id_flag=BlockIdFlag(x["block_id_flag"]),
                validator_address=hexstring_to_bytes(x["validator_address"]),
                timestamp=timestamp_string_to_proto(x["timestamp"]),
                signature=base64.b64decode(x["signature"]) if x["signature"] is not None else None,
              )
              for x in source_current_commit_info["result"]["signed_header"]["commit"]["signatures"]
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
              #proposer_priority=int(x["proposer_priority"]),
            ) for x in source_current_validator_info["validators"]
          ],
          proposer=Validator(
            address=base64.b64decode(bech32_to_b64(proposer_info["address"])),
            pub_key=PublicKey(
              ed25519=base64.b64decode(proposer_info["pub_key"]["key"])
            ) if "ed25519" in proposer_info["pub_key"]["@type"] else PublicKey(
                secp256_k1=base64.b64decode(proposer_info["pub_key"]["key"])
            ),
            voting_power=int(proposer_info["voting_power"]),
            #proposer_priority=int(proposer_info["proposer_priority"]),
          ) 
          ,
          total_voting_power=sum([int(x["voting_power"]) for x in source_current_validator_info["validators"]]),
        ),
        trusted_height=Height(
          revision_number=int(source_client_on_dest_state["client_state"]["latest_height"]["revision_number"]),
          revision_height=int(source_client_on_dest_state["client_state"]["latest_height"]["revision_height"]),
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
              #proposer_priority=int(x["proposer_priority"]),
            ) for x in source_trusted_validator_info["validators"]
          ],
          proposer=Validator(
            address=base64.b64decode(bech32_to_b64(trusted_proposer_info["address"])),
            pub_key=PublicKey(
              ed25519=base64.b64decode(trusted_proposer_info["pub_key"]["key"])
            ) if "ed25519" in trusted_proposer_info["pub_key"]["@type"] else PublicKey(
                secp256_k1=base64.b64decode(trusted_proposer_info["pub_key"]["key"])
            ),
            voting_power=int(trusted_proposer_info["voting_power"]),
            #proposer_priority=int(trusted_proposer_info["proposer_priority"]),
          ) 
          ,
          total_voting_power=sum([int(x["voting_power"]) for x in source_trusted_validator_info["validators"]]),
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
  osmo_client_proof_on_terra = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in osmo_client_proof_on_terra["result"]["response"]["proofOps"]["ops"]]
  client_proof = MerkleProof(proofs=proofs)

  #terra rpc weirdly ignores params for same abci query path w/o sufficient sleep
  time.sleep(2)

  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"connections/{connection_id}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  connection_proof_on_terra = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  connection_proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in connection_proof_on_terra["result"]["response"]["proofOps"]["ops"]]
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
  consensus_proof_on_terra = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  consensus_proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in consensus_proof_on_terra["result"]["response"]["proofOps"]["ops"]]
  consensus_proof = MerkleProof(proofs=consensus_proofs)


  client_state = Any(
    type_url="/ibc.lightclients.tendermint.v1.ClientState",
    value=ClientState.FromString(
      Any.FromString(
        b64_to_bytes(osmo_client_proof_on_terra["result"]["response"]["value"])
      ).value
    ).SerializeToString()
  )

  return (client_proof, connection_proof, consensus_proof, consensus_height, client_state)

###############################################
# connection try
###############################################

#this fails due to mismatching commit hash and header hash; unknown why, but isn't required to move forward
##failed because betterproto fucked up protobuf-converting python datetimes; python datetimes go down to microsecond, protobuf requries nanoseconds
update_osmo_end_msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, terra_client_id)
update_terra_client_on_osmo_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_osmo_end_msg, osmo_wallet, osmo)
header_height = Header.FromString(update_osmo_end_msg.header.value).signed_header.header.height

terra_client_trusted_height = header_height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{terra_client_id}")["client_state"]["latest_height"]["revision_number"]

client_proof, connection_proof, consensus_proof, consensus_height, client_state = fetch_proofs(terra_rpc_url, terra_rpc_header, osmo_client_id, terra_client_trusted_height, terra_client_trusted_revision_number, connection_id)

osmo_client_trusted_height = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{osmo_client_id}")["client_state"]["latest_height"]["revision_height"]
osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{osmo_client_id}")["client_state"]["latest_height"]["revision_number"]

msg = MsgConnectionOpenTry(
  client_id=terra_client_id, #str
  client_state=client_state, #Any
  counterparty=ConnectionCounterParty(
    client_id=osmo_client_id,
    prefix=MerklePrefix(
      key_prefix=bytes("ibc", "ascii"),
    ),
    connection_id=connection_id,
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

###############################################
# connection ack
###############################################

update_terra_end_msg = fabricate_update_client(osmo, osmo_rpc_url, terra_rpc_header, terra, wallet, osmo_client_id)
update_osmo_client_on_terra_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_terra_end_msg, wallet, terra)
header_height = Header.FromString(update_terra_end_msg.header.value).signed_header.header.height

osmo_client_trusted_height = header_height
osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{osmo_client_id}")["client_state"]["latest_height"]["revision_number"]

client_proof, connection_proof, consensus_proof, consensus_height, client_state = fetch_proofs(osmo_rpc_url, terra_rpc_header, terra_client_id, osmo_client_trusted_height, osmo_client_trusted_revision_number, connection_id_on_osmo)

msg = MsgConnectionOpenAck(
  connection_id=connection_id,
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

###############################################
# connection confirm
###############################################

update_osmo_end_msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, terra_client_id)
update_terra_client_on_osmo_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_osmo_end_msg, osmo_wallet, osmo)
header_height = Header.FromString(update_osmo_end_msg.header.value).signed_header.header.height

terra_client_trusted_height = header_height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{terra_client_id}")["client_state"]["latest_height"]["revision_number"]

client_proof, connection_proof, consensus_proof, consensus_height, client_state = fetch_proofs(terra_rpc_url, terra_rpc_header, osmo_client_id, terra_client_trusted_height, terra_client_trusted_revision_number, connection_id)

msg = MsgConnectionOpenConfirm(
  connection_id=connection_id_on_osmo,
  proof_ack=connection_proof.SerializeToString(),
  proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_height)),
  signer=osmo_wallet.key.acc_address,
)

connection_confirm_on_osmo_result = stargate_msg("/ibc.core.connection.v1.MsgConnectionOpenConfirm", msg, osmo_wallet, osmo)
connection_confirm_on_osmo_df = pd.DataFrame(connection_confirm_on_osmo_result["tx_response"]["logs"][0]["events"][0]["attributes"])

###################################################
# setup ica contracts
###################################################

ica_host_code_id = deploy_local_wasm("/repos/cw-ibc-demo/artifacts/simple_ica_host.wasm", wallet, terra)
time.sleep(2)
ica_controller_code_id = deploy_local_wasm("/repos/cw-ibc-demo/artifacts/simple_ica_controller.wasm", wallet, terra)
time.sleep(2)
cw1_code_id = deploy_local_wasm("/repos/cw-plus/artifacts/cw1_whitelist.wasm", wallet, terra)

osmo_ica_host_code_id = deploy_local_wasm("/repos/cw-ibc-demo/artifacts/simple_ica_host.wasm", osmo_wallet, osmo)
time.sleep(2)
osmo_ica_controller_code_id = deploy_local_wasm("/repos/cw-ibc-demo/artifacts/simple_ica_controller.wasm", osmo_wallet, osmo)
time.sleep(2)
osmo_cw1_code_id = deploy_local_wasm("/repos/cw-plus/artifacts/cw1_whitelist.wasm", osmo_wallet, osmo)

#controller
init_msg = {
}
controller_result = init_contract(ica_controller_code_id, init_msg, wallet, terra, "ica_controller")
controller_address = controller_result.logs[0].events_by_type["wasm"]["_contract_address"][0]

#host
init_msg = {
  "cw1_code_id": int(osmo_cw1_code_id),
}
host_result = init_contract(osmo_ica_host_code_id, init_msg, osmo_wallet, osmo, "ica_host")
host_address = host_result.logs[0].events_by_type["instantiate"]["_contract_address"][0]

#contracts automatically generate an ibc port
controller_port = terra.wasm.contract_info(controller_address)["ibc_port_id"]
host_port = osmo.wasm.contract_info(host_address)["ibc_port_id"]

"""
ica_host_code_id = '6176'
ica_controller_code_id = '6177'
cw1_code_id = '6178'

osmo_ica_host_code_id = '4605'
osmo_ica_controller_code_id = '4606'
osmo_cw1_code_id = '4607'
"""


###################################################
# channel open
###################################################

"""
connection_id_on_terra = "connection-161"
client_id_on_terra = "07-tendermint-184"

connection_id_on_osmo = "connection-2617"
client_id_on_osmo = "07-tendermint-3110"
"""

connection_id_on_terra = "connection-166"
client_id_on_terra = "07-tendermint-189"

connection_id_on_osmo = "connection-2705"
client_id_on_osmo = "07-tendermint-3208"


msg = MsgChannelOpenInit(
  port_id=controller_port,
  channel=Channel(
    state=State.STATE_INIT,
    ordering=Order.ORDER_UNORDERED,
    counterparty=Counterparty(
      port_id=host_port,
    ),
    connection_hops=[
      connection_id_on_terra
    ],
    version="simple-ica-v2",
  ),
  signer=wallet.key.acc_address
)

channel_open_init_on_terra = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenInit", msg, wallet, terra)
channel_open_init_on_terra_df = pd.DataFrame(channel_open_init_on_terra["tx_response"]["logs"][0]["events"][0]["attributes"])
channel_id_on_terra = channel_open_init_on_terra_df[channel_open_init_on_terra_df["key"]=="channel_id"]["value"].values[0]

###################################################
# channel try
###################################################

update_client_msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

terra_client_trusted_height = header_height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]


def fetch_channel_proof(rpc_url, rpc_header, port_id, channel_id, trusted_height, trusted_revision_number):

  #client state proof
  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"channelEnds/ports/{port_id}/channels/{channel_id}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  osmo_client_proof_on_terra = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in osmo_client_proof_on_terra["result"]["response"]["proofOps"]["ops"]]
  channel_proof = MerkleProof(proofs=proofs)

  return channel_proof

channel_proof = fetch_channel_proof(terra_rpc_url, terra_rpc_header, controller_port, channel_id_on_terra, terra_client_trusted_height, terra_client_trusted_revision_number)

msg = MsgChannelOpenTry(
  port_id=host_port,
  channel=Channel(
    state=State.STATE_TRYOPEN,
    ordering=Order.ORDER_UNORDERED,
    counterparty=Counterparty(
      port_id=controller_port,
      channel_id=channel_id_on_terra,
    ),
    connection_hops=[
      connection_id_on_osmo
    ],
    version="simple-ica-v2",
  ),
  counterparty_version="simple-ica-v2",
  proof_init=channel_proof.SerializeToString(),
  proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_height)),
  signer=osmo_wallet.key.acc_address,
)

channel_open_try_result = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenTry", msg, osmo_wallet, osmo)
channel_open_try_on_osmo_df = pd.DataFrame(channel_open_try_result["tx_response"]["logs"][0]["events"][0]["attributes"])
channel_id_on_osmo = channel_open_try_on_osmo_df[channel_open_try_on_osmo_df["key"]=="channel_id"]["value"].values[0]

###################################################
# channel ack
###################################################

update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, terra_rpc_header, terra, wallet, client_id_on_terra)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, wallet, terra)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

osmo_client_trusted_height = header_height
osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_terra}")["client_state"]["latest_height"]["revision_number"]

channel_proof = fetch_channel_proof(osmo_rpc_url, terra_rpc_header, host_port, channel_id_on_osmo, osmo_client_trusted_height, osmo_client_trusted_revision_number)

msg = MsgChannelOpenAck(
  port_id=controller_port,
  channel_id=channel_id_on_terra,
  counterparty_channel_id=channel_id_on_osmo,
  counterparty_version="simple-ica-v2",
  proof_try=channel_proof.SerializeToString(),
  proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
  signer=wallet.key.acc_address,
)

time.sleep(2)
channel_open_ack_result = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenAck", msg, wallet, terra)
channel_open_ack_on_terra_df = pd.DataFrame(channel_open_ack_result["tx_response"]["logs"][0]["events"][0]["attributes"])

###################################################
# channel confirm
###################################################

update_client_msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

terra_client_trusted_height = header_height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

channel_proof = fetch_channel_proof(terra_rpc_url, terra_rpc_header, controller_port, channel_id_on_terra, terra_client_trusted_height, terra_client_trusted_revision_number)

msg = MsgChannelOpenConfirm(
  port_id=host_port,
  channel_id=channel_id_on_osmo,
  proof_ack=channel_proof.SerializeToString(),
  proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_height)),
  signer=osmo_wallet.key.acc_address,
)

channel_open_confirm_result = stargate_msg("/ibc.core.channel.v1.MsgChannelOpenConfirm", msg, osmo_wallet, osmo)
channel_open_confirm_on_osmo_df = pd.DataFrame(channel_open_confirm_result["tx_response"]["logs"][0]["events"][0]["attributes"])


###################################################
# dispatch and relay ibc msgs
###################################################

msg = {
  "SendMsgs":{
    "channel_id": ,
    "msgs": [
      {
        "ibc":{
          "send_packet":{
            "channel_id":,
            "data":,
            "timeout":,
          }
        }
      }
    ],
  }
}

###################################################
#fetch queued packets
###################################################

min_height=int(terra.tendermint.block_info()["block"]["header"]["height"]) - 100
max_height=int(terra.tendermint.block_info()["block"]["header"]["height"])

min_height=3038950
max_height=3038970
channel_id_on_osmo = "channel-1839"
channel_id_on_terra = "channel-74"
port_on_osmo = "wasm.osmo1tzdjndknlmjcxunr4sg8q8py4xnh3f6k3ylwpxfy4hvpddr4hdmss8cmzk"
port_on_terra = "wasm.terra1t2da6gvmpf9wuwg8w6ffzusu8m2mfwwthtfm09y4h6x7qnq99lgsyhe2eq"

params = {
  "query": "0x" + bytes(f"send_packet.packet_connection='{connection_id_on_terra}' and tx.height>={min_height} and tx.height<={max_height}", "ascii").hex(),
}
tx_results = requests.get(f"{terra_rpc_url}/tx_search", headers=terra_rpc_header, params=params).json()
parsed_packets = [ (i, b64_to_bytes(z["key"]).decode("utf-8"), b64_to_bytes(z["value"]).decode("utf-8"))
  for (i, y) in enumerate(tx_results["result"]["txs"])
  for x in y["tx_result"]["events"] if x["type"]=="send_packet"
  for z in x["attributes"]
]
packets_df = pd.DataFrame(parsed_packets)
packets_df.columns = ["index", "cols", "vals"]
packets_df = packets_df.pivot(index="index", columns="cols", values="vals")

"""
block txs

time.sleep(2)

params = {
  "query": "0x" + bytes(f"send_packet.packet_connection='{connection_id_on_terra}' and block.height>={min_height} and block.height<={max_height}", "ascii").hex(),
}
block_results = requests.get(f"{terra_rpc_url}/block_search", headers=terra_rpc_header, params=params).json()
"""

params = {
  "path": '"/ibc.core.channel.v1.Query/UnreceivedPackets"',
  "data": "0x" + QueryUnreceivedPacketsRequest(port_on_osmo, channel_id_on_osmo, [int(x) for x in packets_df["packet_sequence"].values]).SerializeToString().hex(),
  "prove": "false",
}
unreceived_packets_sequence_numbers = QueryUnreceivedPacketsResponse.FromString(b64_to_bytes(requests.get(f"{osmo_rpc_url}/abci_query", headers=terra_rpc_header, params=params).json()["result"]["response"]["value"])).sequences

unreceived_packets = packets_df[packets_df["packet_sequence"].isin([str(x) for x in unreceived_packets_sequence_numbers])]

params = [(x, {
  "path": '"/ibc.core.channel.v1.Query/PacketCommitment"',
  "data": "0x" + QueryPacketCommitmentRequest(port_on_terra, channel_id_on_terra, int(x)).SerializeToString().hex(),
  "prove": "false",
}) for x in unreceived_packets["packet_sequence"].values]

unreceived_and_commitment = []

for x in params:

  time.sleep(2)

  _resp = requests.get(f"{terra_rpc_url}/abci_query", headers=terra_rpc_header, params=x[1]).json()["result"]["response"]["value"]

  if _resp is not None:
    unreceived_and_commitment.append((x[0], QueryPacketCommitmentResponse.FromString(b64_to_bytes(_resp))))

valid_packets = unreceived_packets[unreceived_packets["packet_sequence"].isin([x[0] for x in unreceived_and_commitment])]

###################################################
#perform relays
###################################################

#two packet types
##for relay 
##for timeout

valid_packets["timed_out"] = valid_packets["packet_timeout_timestamp"] < str(time.time_ns())
outgoing_packets = valid_packets[valid_packets["timed_out"]==False].reset_index()


#relayed packets

#wait roughly two blocks, then update
time.sleep(10)
update_client_msg = fabricate_update_client(terra, terra_rpc_url, terra_rpc_header, osmo, osmo_wallet, client_id_on_osmo)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, osmo_wallet, osmo)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

terra_client_trusted_height = header_height
terra_client_trusted_revision_number = osmo.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_osmo}")["client_state"]["latest_height"]["revision_number"]

def fetch_packet_proof(rpc_url, rpc_header, trusted_height, trusted_revision_number, packet_row, port_id, channel_id):

  time.sleep(2)

  params = {
    "path": '"/store/ibc/key"',
    "data": "0x" + bytes(f"commitments/ports/{port_id}/channels/{channel_id}/sequences/{packet_row['packet_sequence'][0]}", "ascii").hex(),
    "prove": "true",
    "height": int(trusted_height) - 1,
  }
  osmo_client_proof_on_terra = requests.get(f"{rpc_url}/abci_query", headers=rpc_header, params=params).json()
  proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in osmo_client_proof_on_terra["result"]["response"]["proofOps"]["ops"]]
  packet_proof = MerkleProof(proofs=proofs)

  return packet_proof

packet_proofs = [
  fetch_packet_proof(terra_rpc_url, terra_rpc_header, terra_client_trusted_height, terra_client_trusted_revision_number, x, port_on_terra, channel_id_on_terra)

  for (i,x) in outgoing_packets.iterrows()
]

i = 0
outgoing_packet = outgoing_packets.iloc[i, :]

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
  proof_commitment=packet_proofs[i].SerializeToString(),
  proof_height=Height(int(terra_client_trusted_revision_number), int(terra_client_trusted_height)),
  signer=osmo_wallet.key.acc_address
)

relay_result = stargate_msg("/ibc.core.channel.v1.MsgRecvPacket", msg, osmo_wallet, osmo)
relay_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_result["tx_response"]["logs"][0]["events"]] for y in x])

#sleep for a bit for indexers to catch up

#fetch pending acks
##fetch acks from tx search; query is write_acknowledgement.packet_connection=


min_height=int(osmo.tendermint.block_info()["block"]["header"]["height"]) - 100
max_height=int(osmo.tendermint.block_info()["block"]["header"]["height"])

min_height=3038950
max_height=3038970

params = {
  "query": "0x" + bytes(f"write_acknowledgement.packet_connection='{connection_id_on_osmo}' and tx.height>={min_height} and tx.height<={max_height}", "ascii").hex(),
}
tx_results = requests.get(f"{osmo_rpc_url}/tx_search", headers=terra_rpc_header, params=params).json()
parsed_acks = [ (i, b64_to_bytes(z["key"]).decode("utf-8"), b64_to_bytes(z["value"]).decode("utf-8"))
  for (i, y) in enumerate(tx_results["result"]["txs"])
  for x in y["tx_result"]["events"] if x["type"]=="write_acknowledgement"
  for z in x["attributes"]
]
acks_df = pd.DataFrame(parsed_acks)
acks_df.columns = ["index", "cols", "vals"]
acks_df = acks_df.pivot(index="index", columns="cols", values="vals")

##for each ack, check if unreceived via QueryUnreceivedAcksRequest
##note acks dont timeout

i = 0
ack = acks_df.iloc[i,:]

params = {
  "path": '"/ibc.core.channel.v1.Query/UnreceivedAcks"',
  "data": "0x" + QueryUnreceivedAcksRequest(port_on_terra, channel_id_on_terra, list(acks_df["packet_sequence"].map(lambda x: int(x)).values)).SerializeToString().hex(),
  "prove": "false",
}
unreceived_acks = QueryUnreceivedAcksResponse.FromString(b64_to_bytes(requests.get(f"{terra_rpc_url}/abci_query", headers=terra_rpc_header, params=params).json()["result"]["response"]["value"])).sequences


##update client
update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, terra_rpc_header, terra, wallet, client_id_on_terra)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, wallet, terra)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

osmo_client_trusted_height = header_height
osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_terra}")["client_state"]["latest_height"]["revision_number"]


##fetch packetack proofs - acks/ports/{port_id}/channels/{channel_id}/sequences/{ack_sequence}
params = {
  "path": '"/store/ibc/key"',
  "data": "0x" + bytes(f"acks/ports/{ack.packet_dst_port}/channels/{ack.packet_dst_channel}/sequences/{ack.packet_sequence}", "ascii").hex(),
  "prove": "true",
  "height": int(osmo_client_trusted_height) - 1,
}
osmo_client_proof_on_terra = requests.get(f"{osmo_rpc_url }/abci_query", headers=terra_rpc_header, params=params).json()
proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in osmo_client_proof_on_terra["result"]["response"]["proofOps"]["ops"]]
ack_proof = MerkleProof(proofs=proofs)

##fabricate MsgAcknowledgement
msg = MsgAcknowledgement(
  packet=packet,
  acknowledgement=hexstring_to_bytes(ack["packet_ack_hex"]),
  proof_acked=ack_proof.SerializeToString(),
  proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
  signer=wallet.key.acc_address,
)

relay_ack_result = stargate_msg("/ibc.core.channel.v1.MsgAcknowledgement", msg, wallet, terra)
relay_ack_result_df = pd.DataFrame([y for x in [x["attributes"] for x in relay_ack_result["tx_response"]["logs"][0]["events"]] for y in x])


#timeout packets
update_client_msg = fabricate_update_client(osmo, osmo_rpc_url, terra_rpc_header, terra, wallet, client_id_on_terra)
update_client_before_channel_try_result = stargate_msg("/ibc.core.client.v1.MsgUpdateClient", update_client_msg, wallet, terra)
header_height = Header.FromString(update_client_msg.header.value).signed_header.header.height

osmo_client_trusted_height = header_height
osmo_client_trusted_revision_number = terra.broadcaster.query(f"/ibc/core/client/v1/client_states/{client_id_on_terra}")["client_state"]["latest_height"]["revision_number"]

timed_out_packets = valid_packets[valid_packets["timed_out"]].reset_index()
i=0

timed_out_packet = timed_out_packets.iloc[i, :]

packet = Packet(
  sequence=int(timed_out_packet["packet_sequence"]),
  source_port=timed_out_packet["packet_src_port"],
  source_channel=timed_out_packet["packet_src_channel"],
  destination_port=timed_out_packet["packet_dst_port"],
  destination_channel=timed_out_packet["packet_dst_channel"],
  data=hexstring_to_bytes(timed_out_packet.packet_data_hex),
  timeout_height=Height(int(timed_out_packet["packet_timeout_height"].split('-')[0]), int(timed_out_packet["packet_timeout_height"].split('-')[1])),
  timeout_timestamp=int(timed_out_packet["packet_timeout_timestamp"]),
)

params = {
  "path": '"/ibc.core.channel.v1.Query/NextSequenceReceive"',
  "data": "0x" + QueryNextSequenceReceiveRequest(port_on_osmo, channel_id_on_osmo).SerializeToString().hex(),
  "prove": "false",
}
next_sequence_number = QueryNextSequenceReceiveResponse.FromString(b64_to_bytes(requests.get(f"{osmo_rpc_url}/abci_query", headers=terra_rpc_header, params=params).json()["result"]["response"]["value"])).next_sequence_receive

params = {
  "path": '"/store/ibc/key"',
  "data": "0x" + bytes(f"receipts/ports/{packet.destination_port}/channels/{packet.destination_channel}/sequences/{packet.sequence}", "ascii").hex(),
  "prove": "true",
  "height": int(osmo_client_trusted_height) - 1,
}
osmo_client_proof_on_terra = requests.get(f"{osmo_rpc_url }/abci_query", headers=terra_rpc_header, params=params).json()
proofs = [CommitmentProof.FromString(b64_to_bytes(x["data"])) for x in osmo_client_proof_on_terra["result"]["response"]["proofOps"]["ops"]]
receipt_proof = MerkleProof(proofs=proofs)

#for each timed-out packet
msg = MsgTimeout(
  packet=packet,
  proof_unreceived=receipt_proof.SerializeToString(),
  proof_height=Height(int(osmo_client_trusted_revision_number), int(osmo_client_trusted_height)),
  next_sequence_recv=next_sequence_number,
  signer=wallet.key.acc_address,
)

timeout_result = stargate_msg("/ibc.core.channel.v1.MsgTimeout", msg, wallet, terra)
timeout_result_df = pd.DataFrame([y for x in [x["attributes"] for x in timeout_result["tx_response"]["logs"][0]["events"]] for y in x])
