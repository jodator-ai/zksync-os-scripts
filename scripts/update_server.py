#!/usr/bin/env python3
"""
Local state update script for zksync-os-server.

Steps:
- Check env + tooling
- Build zkstack CLI
- Build era-contracts L1, generate genesis.json
- Initialize ecosystem (zksync-os mode)
- Start Anvil, fund accounts, deploy L1 contracts
- Extract bridgehub + operator keys
- Generate L1 -> L2 deposit tx
- Stop Anvil and dump the new zkos-l1-state.json

Additional env vars:

  CHAIN_IDS   Comma-separated chain IDs to generate.  Default: "6565,6566".
              Example: CHAIN_IDS=6565,6566,6567,6568

  OUTPUT_DIR  If set, chain configs, wallets, contracts and l1-state.json.gz
              are written here instead of the default
              <REPO_DIR>/local-chains/<version>/multi_chain/ path.
              Useful for Docker-based or CI usage where you don't want to
              modify the server repo in-place.
"""

import os
import yaml as _yaml
from pathlib import Path
from packaging.version import Version

from lib.script_context import ScriptCtx
from lib.entry import run_script
from lib import utils
from lib import edit_server
from lib import config
from lib.protocol_version import (
    PROTOCOL_TOOLCHAINS,
    PROTOCOL_VERSION_NEXT,
)


# ---------------------------------------------------------------------------
# Funding logic
# ---------------------------------------------------------------------------
def fund_accounts(ctx: ScriptCtx, ecosystem_dir: Path) -> None:
    """
    Approximate port of the bash funding logic:
    - Find all wallets.yaml
    - For each, extract addresses and send 100 ETH
    - Then fund two (hardcoded) rich wallets with 9000 ETH each
    """

    if not ecosystem_dir.is_dir():
        ctx.fail(f"Ecosystem dir not found: {ecosystem_dir}")

    wallets_files = list(ecosystem_dir.rglob("wallets.yaml"))
    if not wallets_files:
        ctx.fail(f"No wallets.yaml found under {ecosystem_dir}")

    all_addrs: set[str] = set()
    for wf in wallets_files:
        data = utils.load_yaml(wf)
        addrs = utils.addresses_from_wallets_yaml(data)
        if addrs:
            ctx.logger.debug(f"Found {len(addrs)} addresses in {wf}")
            all_addrs.update(addrs)

    rpc_url: str = config.ANVIL_DEFAULT_URL

    # Fund each address
    ctx.logger.debug(f"Funding {len(all_addrs)} addresses with 100 ETH each...")
    amount_100eth = hex(100 * 10**18)
    for addr in sorted(all_addrs):
        ctx.sh(
            f"cast rpc anvil_setBalance {addr} {amount_100eth} --rpc-url {rpc_url}",
            print_command=False,
        )

    # Two large transfers between rich wallets
    ctx.logger.debug("Funding two rich wallets with 9000 ETH each...")
    amount_9000eth = hex(9000 * 10**18)
    ctx.sh(
        f"cast rpc anvil_setBalance 0xa61464658afeaf65cccaafd3a512b69a83b77618 {amount_9000eth} --rpc-url {rpc_url}",
        print_command=False,
    )
    ctx.sh(
        f"cast rpc anvil_setBalance 0x36615cf349d7f6344891b1e7ca7c72883f5dc049 {amount_9000eth} --rpc-url {rpc_url}",
        print_command=False,
    )


def _parse_chain_ids() -> list[str]:
    """
    Read chain IDs from the CHAIN_IDS env var (comma-separated).
    Falls back to the two pre-configured default chains.
    """
    raw = os.environ.get("CHAIN_IDS", "6565,6566")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _chain_config_template(chain: str, rpc_port: int, protocol_version: str) -> dict:
    """
    Minimal chain_XXXX.yaml template for a chain ID that has no pre-existing
    config in the server repo.  Address and key fields are zero-filled
    placeholders — they are patched by update_chain_config_yaml() after
    deployment completes.
    """
    zero_addr = "0x" + "0" * 40
    zero_key  = "0x" + "0" * 64
    return {
        "general": {"ephemeral": False, "rocks_db_path": "/db/node1"},
        "genesis": {
            "bridgehub_address": zero_addr,
            "bytecode_supplier_address": zero_addr,
            "genesis_input_path": f"./local-chains/{protocol_version}/genesis.json",
            "chain_id": int(chain),
        },
        "l1_sender": {
            "pubdata_mode": "Blobs",
            "operator_commit_sk": zero_key,
            "operator_prove_sk": zero_key,
            "operator_execute_sk": zero_key,
        },
        "rpc": {"address": f"0.0.0.0:{rpc_port}"},
        "external_price_api_client": {
            "source": "Forced",
            "forced_prices": {"0x0000000000000000000000000000000000000001": 3000},
        },
    }


def init_ecosystem(
    ctx: ScriptCtx,
    ecosystem_name: str,
    chains: list[str],
) -> None:
    era_contracts_path = utils.require_path("ERA_CONTRACTS_PATH")
    zksync_era_path = utils.require_path("ZKSYNC_ERA_PATH")
    protocol_version = utils.require_env("PROTOCOL_VERSION")

    zkstack_bin = zksync_era_path / "zkstack_cli" / "target" / "release" / "zkstack"
    ecosystems_dir = ctx.workspace / "ecosystems"
    ecosystem_dir = ctx.workspace / "ecosystems" / ecosystem_name
    protocol_base = ctx.repo_dir / "local-chains" / protocol_version
    default_base = protocol_base / "default"

    # OUTPUT_DIR overrides where outputs land; when set, skip updating default/.
    output_dir_env = os.environ.get("OUTPUT_DIR")
    base = Path(output_dir_env) if output_dir_env else protocol_base / ecosystem_name
    base.mkdir(parents=True, exist_ok=True)

    with ctx.section(f"Initialize {ecosystem_name} ecosystem", expected=120):
        utils.clean_dir(ecosystem_dir)
        ctx.sh(
            f"""
                {zkstack_bin}
                  ecosystem create
                  --ecosystem-name {ecosystem_name}
                  --l1-network localhost
                  --chain-name tmp-chain
                  --chain-id 12345
                  --prover-mode no-proofs
                  --wallet-creation random
                  --link-to-code {zksync_era_path}
                  --l1-batch-commit-data-generator-mode rollup
                  --start-containers false
                  --base-token-address 0x0000000000000000000000000000000000000001
                  --base-token-price-nominator 1
                  --base-token-price-denominator 1
                  --evm-emulator false
                """,
            cwd=ecosystems_dir,
        )
        ctx.sh(
            f"""
                {zkstack_bin}
                  ctm set-ctm-contracts
                  --contracts-src-path {era_contracts_path}
                  --default-configs-src-path {ctx.repo_dir / "local-chains" / protocol_version / "default"}
                  --zksync-os
                """,
            cwd=ecosystem_dir,
        )
        # Remove default era chain (non zksync-os)
        utils.clean_dir(ecosystem_dir / "chains")

        for chain in chains:
            ctx.sh(
                f"""
                {zkstack_bin}
                  chain create
                  --chain-name {chain}
                  --chain-id {chain}
                  --prover-mode no-proofs
                  --wallet-creation random
                  --l1-batch-commit-data-generator-mode rollup
                  --base-token-address 0x0000000000000000000000000000000000000001
                  --base-token-price-nominator 1
                  --base-token-price-denominator 1
                  --evm-emulator false
                  --set-as-default=true
                  --zksync-os
                """,
                cwd=ecosystem_dir,
            )

    # ------------------------------------------------------------------ #
    # Start Anvil
    # ------------------------------------------------------------------ #
    with ctx.section(f"Generating l1-state.json for {ecosystem_name}", expected=250):
        l1_state_file = base / "l1-state.json" if output_dir_env else protocol_base / "l1-state.json"
        with utils.anvil_dump_state(l1_state_file=l1_state_file):
            # ------------------------------------------------------------------ #
            # Fund accounts
            # ------------------------------------------------------------------ #
            ctx.logger.info("Funding accounts...")
            fund_accounts(ctx, ecosystem_dir)
            # ------------------------------------------------------------------ #
            # Deploy L1 contracts via zkstack
            # ------------------------------------------------------------------ #
            ctx.logger.info("Deploying L1 contracts...")
            ctx.sh(
                f"""
                    {zkstack_bin}
                      ecosystem init
                      --deploy-paymaster=false
                      --deploy-erc20=false
                      --observability=false
                      --no-port-reallocation
                      --deploy-ecosystem
                      --l1-rpc-url="{config.ANVIL_DEFAULT_URL}"
                      --zksync-os
                    """,
                cwd=ecosystem_dir,
            )
            for i, chain in enumerate(chains):
                # ------------------------------------------------------------------ #
                # Ensure chain config template exists
                # Pre-configured chains (6565, 6566) already have a file in base/.
                # New chain IDs get a minimal template created here; it is patched
                # with real addresses and keys by update_chain_config_yaml() below.
                # ------------------------------------------------------------------ #
                chain_yaml = base / f"chain_{chain}.yaml"
                if not chain_yaml.exists():
                    ctx.logger.info(f"No existing config for chain {chain} — generating template")
                    rpc_port = 3050 + i
                    with chain_yaml.open("w", encoding="utf-8") as f:
                        _yaml.safe_dump(
                            _chain_config_template(chain, rpc_port, protocol_version),
                            f,
                            default_flow_style=False,
                            sort_keys=False,
                        )
                        f.write("\n")
                # ------------------------------------------------------------------ #
                # Update contract addresses and operator keys
                # ------------------------------------------------------------------ #
                ctx.logger.debug("Updating contract addresses...")
                contracts_yaml = (
                    ecosystem_dir / "chains" / chain / "configs" / "contracts.yaml"
                )
                chain_wallets_yaml = (
                    ecosystem_dir / "chains" / chain / "configs" / "wallets.yaml"
                )
                edit_server.update_chain_config_yaml(
                    chain_yaml,
                    contracts_yaml=contracts_yaml,
                    wallets_yaml=chain_wallets_yaml,
                )
                name_suffix = f"_{chain}" if ecosystem_name == "multi_chain" else ""
                wallets_out = base / f"wallets{name_suffix}.yaml"
                contracts_out = base / f"contracts{name_suffix}.yaml"
                # Copy wallets.yaml and contracts.yaml to local-chains
                utils.cp(chain_wallets_yaml, wallets_out)
                utils.cp(contracts_yaml, contracts_out)
                # ------------------------------------------------------------------ #
                # Generate deposit transaction
                # ------------------------------------------------------------------ #
                if not os.environ.get("SKIP_DEPOSIT_TX"):
                    ctx.logger.info("Generating L1 -> L2 deposit transaction...")
                    bridgehub_address = edit_server.get_contract_address(
                        contracts_yaml,
                        "bridgehub_proxy_addr",
                    )
                    ctx.sh(
                        f"""
                        cargo run --release --package zksync_os_generate_deposit --
                        --bridgehub "{bridgehub_address}"
                        --chain-id {chain}
                        --amount 100
                        """
                    )
                if chain == config.GATEWAY_CHAIN_ID:
                    ctx.sh(
                        f"""
                            {zkstack_bin}
                            chain gateway create-tx-filterer
                            --chain {config.GATEWAY_CHAIN_ID}
                            --l1-rpc-url="{config.ANVIL_DEFAULT_URL}"
                            --ignore-prerequisites
                            """,
                        cwd=ecosystem_dir,
                    )
                    ctx.sh(
                        f"""
                            {zkstack_bin}
                            chain gateway convert-to-gateway
                            --chain {config.GATEWAY_CHAIN_ID}
                            --l1-rpc-url="{config.ANVIL_DEFAULT_URL}"
                            --ignore-prerequisites
                            --no-gateway-overrides
                            """,
                        cwd=ecosystem_dir,
                    )
            # Update Default setup with information from the first chain in the list.
            # Skip when OUTPUT_DIR is set — there is no server-repo default/ to update.
            # TODO: temporarily we are reusing one of the chains from Multichain setup for the Default setup
            if not output_dir_env:
                contracts_yaml = (
                    ecosystem_dir / "chains" / chains[0] / "configs" / "contracts.yaml"
                )
                chain_wallets_yaml = (
                    ecosystem_dir / "chains" / chains[0] / "configs" / "wallets.yaml"
                )
                edit_server.update_chain_config_yaml(
                    default_base / "config.yaml",
                    contracts_yaml=contracts_yaml,
                    wallets_yaml=chain_wallets_yaml,
                )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def script(ctx: ScriptCtx) -> None:
    # Paths & constants
    era_contracts_path: Path = utils.require_path("ERA_CONTRACTS_PATH")
    zksync_era_path: Path = utils.require_path("ZKSYNC_ERA_PATH")
    protocol_version: str = utils.require_env("PROTOCOL_VERSION")
    try:
        toolchain = PROTOCOL_TOOLCHAINS[protocol_version]
    except KeyError:
        raise ValueError(
            f"Unsupported PROTOCOL_VERSION: {protocol_version}. Supported: {list(PROTOCOL_TOOLCHAINS.keys())}"
        )
    execution_version: str = toolchain.execution_version
    proving_version: str = toolchain.proving_version
    cast_forge_version: str = toolchain.cast_forge_version
    anvil_version: str = toolchain.anvil_version
    cargo_version: str = toolchain.cargo_version
    yarn_version: str = toolchain.yarn_version

    skip_build = bool(os.environ.get("SKIP_BUILD"))
    skip_deposit_tx = bool(os.environ.get("SKIP_DEPOSIT_TX"))

    # ------------------------------------------------------------------ #
    # Tooling check
    # ------------------------------------------------------------------ #
    if not skip_build:
        utils.require_cmds(
            {
                "yarn": f">={yarn_version}",
                "anvil": f"=={anvil_version}",
                "cast": f"=={cast_forge_version}",
                "forge": f"=={cast_forge_version}",
                "cargo": f">={cargo_version}",
            }
        )

    # TODO: remove this later, needs only for v31 for now
    # ------------------------------------------------------------------ #
    # Build contracts for zkstack (temporary)
    # ------------------------------------------------------------------ #
    if not skip_build and Version(protocol_version) >= Version(PROTOCOL_VERSION_NEXT):
        zkstack_era_contracts_path: Path = zksync_era_path / "contracts"
        with ctx.section("Build contracts in zkstack", expected=120):
            ctx.sh(
                """
                yarn install
                """,
                cwd=zkstack_era_contracts_path,
            )
            ctx.sh(
                """
                yarn build:foundry
                """,
                cwd=zkstack_era_contracts_path / "da-contracts",
            )
            ctx.sh(
                """
                yarn build:foundry
                """,
                cwd=zkstack_era_contracts_path / "l1-contracts",
            )

    if not skip_build:
        # ------------------------------------------------------------------ #
        # Build contracts
        # ------------------------------------------------------------------ #
        with ctx.section("Build contracts", expected=120):
            ctx.sh(
                """
                yarn install
                """,
                cwd=era_contracts_path,
            )
            ctx.sh(
                """
                yarn build:foundry
                """,
                cwd=era_contracts_path / "da-contracts",
            )
            ctx.sh(
                """
                yarn build:foundry
                """,
                cwd=era_contracts_path / "l1-contracts",
            )

        # ------------------------------------------------------------------ #
        # Build zkstack CLI
        # ------------------------------------------------------------------ #
        with ctx.section("Build zkstack CLI", expected=100):
            ctx.sh(
                """
                cargo build --release --bin zkstack
                """,
                cwd=zksync_era_path / "zkstack_cli",
            )

        # ------------------------------------------------------------------ #
        # Generate genesis.json
        # ------------------------------------------------------------------ #
        with ctx.section("Generate genesis.json", expected=60):
            ctx.sh(
                f"""
                cargo run --
                  --output-file {ctx.repo_dir / "local-chains" / protocol_version / "genesis.json"}
                  --execution-version {execution_version}
                """,
                cwd=era_contracts_path / "tools" / "zksync-os-genesis-gen",
            )

    # TODO: currently single-chain setup is disabled, instead it is a symlink to one of the chains from Multi-chain setup
    #       this might change in the future
    # # ------------------------------------------------------------------ #
    # # Single-chain setup
    # # ------------------------------------------------------------------ #
    # init_ecosystem(ctx, "default", ["6565"])

    # ------------------------------------------------------------------ #
    # Multi-chain setup
    # ------------------------------------------------------------------ #
    # TODO: uncomment when gateway chain is supported in main server
    init_ecosystem(ctx, "multi_chain", _parse_chain_ids())
    # if Version(protocol_version) == Version(PROTOCOL_VERSION_CURRENT):
    #     init_ecosystem(ctx, "multi_chain", _parse_chain_ids())

    # if Version(protocol_version) >= Version(PROTOCOL_VERSION_NEXT):
    #     init_ecosystem(ctx, "multi_chain", _parse_chain_ids() + [config.GATEWAY_CHAIN_ID])

    if not skip_build:
        # ------------------------------------------------------------------ #
        # Update VK hash in prover config
        # ------------------------------------------------------------------ #
        edit_server.update_vk_hash(
            ctx.repo_dir / "lib" / "types" / "src" / "protocol" / "proving_version.rs",
            era_contracts_path
            / "l1-contracts"
            / "contracts"
            / "state-transition"
            / "verifiers"
            / "ZKsyncOSVerifierPlonk.sol",
            proving_version,
        )

        # ------------------------------------------------------------------ #
        # Regenerate contracts.json
        # ------------------------------------------------------------------ #
        with ctx.section("Regenerate contracts.json", expected=30):
            ctx.sh("yarn install", cwd=era_contracts_path / "l1-contracts")
            ctx.sh(
                f"""
                yarn write-factory-deps-zksync-os
                --output {ctx.repo_dir}/lib/l1_watcher/src/factory_deps/contracts.json
                """,
                cwd=era_contracts_path / "l1-contracts",
            )


if __name__ == "__main__":
    run_script(
        script,
        required_env=(
            "ERA_CONTRACTS_PATH",
            "ZKSYNC_ERA_PATH",
            "PROTOCOL_VERSION",
        ),
    )
