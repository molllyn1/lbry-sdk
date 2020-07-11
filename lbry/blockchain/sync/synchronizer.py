import os
import asyncio
import logging
from functools import partial
from typing import Optional, Tuple, Set, List, Coroutine

from lbry.db import Database
from lbry.db import queries as q
from lbry.db.constants import TXO_TYPES, CONTENT_TYPE_CODES
from lbry.db.query_context import Event, Progress
from lbry.event import BroadcastSubscription
from lbry.service.base import Sync, BlockEvent
from lbry.blockchain.lbrycrd import Lbrycrd

from . import blocks as block_phase, claims as claim_phase, supports as support_phase


log = logging.getLogger(__name__)

BLOCK_INIT_EVENT = Event.add("blockchain.sync.block.init", "steps")
BLOCK_MAIN_EVENT = Event.add("blockchain.sync.block.main", "blocks", "txs")
FILTER_INIT_EVENT = Event.add("blockchain.sync.filter.init", "steps")
FILTER_MAIN_EVENT = Event.add("blockchain.sync.filter.main", "blocks")
CLAIM_INIT_EVENT = Event.add("blockchain.sync.claims.init", "steps")
CLAIM_MAIN_EVENT = Event.add("blockchain.sync.claims.main", "claims")
SUPPORT_INIT_EVENT = Event.add("blockchain.sync.supports.init", "steps")
SUPPORT_MAIN_EVENT = Event.add("blockchain.sync.supports.main", "supports")
TREND_INIT_EVENT = Event.add("blockchain.sync.trends.init", "steps")
TREND_MAIN_EVENT = Event.add("blockchain.sync.trends.main", "blocks")


class BlockchainSync(Sync):

    TX_FLUSH_SIZE = 20_000  # flush to db after processing this many TXs and update progress
    FILTER_CHUNK_SIZE = 100_000  # split filter generation tasks into this size block chunks
    FILTER_FLUSH_SIZE = 10_000  # flush to db after processing this many filters and update progress
    CLAIM_CHUNK_SIZE = 50_000  # split claim sync tasks into this size block chunks
    CLAIM_FLUSH_SIZE = 10_000  # flush to db after processing this many claims and update progress
    SUPPORT_CHUNK_SIZE = 50_000  # split support sync tasks into this size block chunks
    SUPPORT_FLUSH_SIZE = 10_000  # flush to db after processing this many supports and update progress

    def __init__(self, chain: Lbrycrd, db: Database):
        super().__init__(chain.ledger, db)
        self.chain = chain
        self.pid = os.getpid()
        self.on_block_subscription: Optional[BroadcastSubscription] = None
        self.advance_loop_task: Optional[asyncio.Task] = None
        self.advance_loop_event = asyncio.Event()

    async def start(self):
        self.advance_loop_task = asyncio.create_task(self.advance())
        await self.advance_loop_task
        self.chain.subscribe()
        self.advance_loop_task = asyncio.create_task(self.advance_loop())
        self.on_block_subscription = self.chain.on_block.listen(
            lambda e: self.advance_loop_event.set()
        )

    async def stop(self):
        self.chain.unsubscribe()
        if self.on_block_subscription is not None:
            self.on_block_subscription.cancel()
        self.db.stop_event.set()
        if self.advance_loop_task is not None:
            self.advance_loop_task.cancel()

    async def run_tasks(self, tasks: List[Coroutine]) -> Optional[Set[asyncio.Future]]:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )
        if pending:
            self.db.stop_event.set()
            for future in pending:
                future.cancel()
            for future in done:
                future.result()
            return
        return done

    async def get_best_block_height_for_file(self, file_number) -> int:
        return await self.db.run(
            block_phase.get_best_block_height_for_file, file_number
        )

    async def sync_blocks(self) -> Optional[Tuple[int, int]]:
        tasks = []
        starting_height = None
        tx_count = block_count = 0
        with Progress(self.db.message_queue, BLOCK_INIT_EVENT) as p:
            ending_height = await self.chain.db.get_best_height()
            for chain_file in p.iter(await self.chain.db.get_block_files()):
                # block files may be read and saved out of order, need to check
                # each file individually to see if we have missing blocks
                our_best_file_height = await self.get_best_block_height_for_file(
                    chain_file['file_number']
                )
                if our_best_file_height == chain_file['best_height']:
                    # we have all blocks in this file, skipping
                    continue
                if -1 < our_best_file_height < chain_file['best_height']:
                    # we have some blocks, need to figure out what we're missing
                    # call get_block_files again limited to this file and current_height
                    chain_file = (await self.chain.db.get_block_files(
                        file_number=chain_file['file_number'], start_height=our_best_file_height+1,
                    ))[0]
                tx_count += chain_file['txs']
                block_count += chain_file['blocks']
                starting_height = min(
                    our_best_file_height+1 if starting_height is None else starting_height, our_best_file_height+1
                )
                tasks.append(self.db.run(
                    block_phase.sync_block_file, chain_file['file_number'], our_best_file_height+1,
                    chain_file['txs'], self.TX_FLUSH_SIZE
                ))
        with Progress(self.db.message_queue, BLOCK_MAIN_EVENT) as p:
            p.start(block_count, tx_count, extra={
                "starting_height": starting_height,
                "ending_height": ending_height,
                "files": len(tasks),
                "claims": await self.chain.db.get_claim_metadata_count(starting_height, ending_height),
                "supports": await self.chain.db.get_support_metadata_count(starting_height, ending_height),
            })
            completed = await self.run_tasks(tasks)
            if completed:
                best_height_processed = max(f.result() for f in completed)
                return starting_height, best_height_processed

    async def sync_filters(self):
        if not self.conf.spv_address_filters:
            return
        with Progress(self.db.message_queue, FILTER_MAIN_EVENT) as p:
            blocks = 0
            tasks = []
            # for chunk in range(select min(height), max(height) from block where filter is null):
            #     tasks.append(self.db.run(block_phase.sync_filters, chunk))
            p.start(blocks)
            await self.run_tasks(tasks)

    async def sync_txios(self, blocks_added):
        if blocks_added:
            await self.db.run(block_phase.sync_txoi, blocks_added[0] == 0)

    async def count_unspent_txos(
        self,
        txo_types: Tuple[int, ...],
        blocks: Tuple[int, int] = None,
        missing_in_supports_table: bool = False,
        missing_in_claims_table: bool = False,
        missing_or_stale_in_claims_table: bool = False,
    ) -> int:
        return await self.db.run(
            q.count_unspent_txos, txo_types, blocks,
            missing_in_supports_table,
            missing_in_claims_table,
            missing_or_stale_in_claims_table,
        )

    async def distribute_unspent_txos(
        self,
        txo_types: Tuple[int, ...],
        blocks: Tuple[int, int] = None,
        missing_in_supports_table: bool = False,
        missing_in_claims_table: bool = False,
        missing_or_stale_in_claims_table: bool = False,
    ) -> int:
        return await self.db.run(
            q.distribute_unspent_txos, txo_types, blocks,
            missing_in_supports_table,
            missing_in_claims_table,
            missing_or_stale_in_claims_table,
        )

    async def count_abandoned_supports(self) -> int:
        return await self.db.run(q.count_abandoned_supports)

    async def count_abandoned_claims(self) -> int:
        return await self.db.run(q.count_abandoned_claims)

    async def count_claims_with_changed_supports(self, blocks) -> int:
        return await self.db.run(q.count_claims_with_changed_supports, blocks)

    async def count_channels_with_changed_content(self, blocks) -> int:
        return await self.db.run(q.count_channels_with_changed_content, blocks)

    async def count_takeovers(self, blocks) -> int:
        return await self.chain.db.get_takeover_count(
            start_height=blocks[0], end_height=blocks[-1]
        )

    async def sync_claims(self, blocks):
        total = delete_claims = takeovers = claims_with_changed_supports = 0
        initial_sync = not await self.db.has_claims()
        with Progress(self.db.message_queue, CLAIM_INIT_EVENT) as p:
            if initial_sync:
                p.start(2)
                # 1. distribute channel insertion load
                channels, channel_batches = await self.distribute_unspent_txos(TXO_TYPES['channel'])
                channels_with_changed_content = channels
                total += channels + channels_with_changed_content
                p.step()
                # 2. distribute content insertion load
                content, content_batches = await self.distribute_unspent_txos(CONTENT_TYPE_CODES)
                total += content
                p.step()
            elif blocks:
                p.start(6)
                # 1. channel claims to be inserted or updated
                channels = await self.count_unspent_txos(
                    TXO_TYPES['channel'], blocks, missing_or_stale_in_claims_table=True
                )
                channel_batches = [blocks] if channels else []
                total += channels
                p.step()
                # 2. content claims to be inserted or updated
                content = await self.count_unspent_txos(
                    CONTENT_TYPE_CODES, blocks, missing_or_stale_in_claims_table=True
                )
                content_batches = [blocks] if content else []
                total += content
                p.step()
                # 3. claims to be deleted
                delete_claims = await self.count_abandoned_claims()
                total += delete_claims
                p.step()
                # 4. claims to be updated with new support totals
                claims_with_changed_supports = await self.count_claims_with_changed_supports(blocks)
                total += claims_with_changed_supports
                p.step()
                # 5. channels to be updated with changed content totals
                channels_with_changed_content = await self.count_channels_with_changed_content(blocks)
                total += channels_with_changed_content
                p.step()
                # 6. claims to be updated due to name takeovers
                takeovers = await self.count_takeovers(blocks)
                total += takeovers
                p.step()
            else:
                return
        with Progress(self.db.message_queue, CLAIM_MAIN_EVENT) as p:
            p.start(total)
            insertions = [
                (TXO_TYPES['channel'], channel_batches),
                (CONTENT_TYPE_CODES, content_batches),
            ]
            for txo_type, batches in insertions:
                if batches:
                    await self.run_tasks([
                        self.db.run(
                            claim_phase.claims_insert, txo_type, batch, not initial_sync
                        ) for batch in batches
                    ])
                    if not initial_sync:
                        await self.run_tasks([
                            self.db.run(claim_phase.claims_update, txo_type, batch)
                            for batch in batches
                        ])
            if delete_claims:
                await self.db.run(claim_phase.claims_delete, delete_claims)
            if takeovers:
                await self.db.run(claim_phase.update_takeovers, blocks, takeovers)
            if claims_with_changed_supports:
                await self.db.run(claim_phase.update_stakes, blocks, claims_with_changed_supports)
            if channels_with_changed_content:
                return initial_sync, channels_with_changed_content

    async def sync_supports(self, blocks):
        delete_supports = 0
        initial_sync = not await self.db.has_supports()
        with Progress(self.db.message_queue, SUPPORT_INIT_EVENT) as p:
            if initial_sync:
                total, support_batches = await self.distribute_unspent_txos(TXO_TYPES['support'])
            elif blocks:
                p.start(2)
                # 1. supports to be inserted
                total = await self.count_unspent_txos(
                    TXO_TYPES['support'], blocks, missing_in_supports_table=True
                )
                support_batches = [blocks] if total else []
                p.step()
                # 2. supports to be deleted
                delete_supports = await self.count_abandoned_supports()
                total += delete_supports
                p.step()
            else:
                return
        with Progress(self.db.message_queue, SUPPORT_MAIN_EVENT) as p:
            p.start(total)
            if support_batches:
                await self.run_tasks([
                    self.db.run(
                        support_phase.supports_insert, batch, not initial_sync
                    ) for batch in support_batches
                ])
            if delete_supports:
                await self.db.run(support_phase.supports_delete, delete_supports)

    async def sync_channel_stats(self, blocks, initial_sync, channels_with_changed_content):
        if channels_with_changed_content:
            await self.db.run(
                claim_phase.update_channel_stats, blocks, initial_sync, channels_with_changed_content
            )

    async def sync_trends(self):
        pass

    async def advance(self):
        blocks_added = await self.sync_blocks()
        sync_filters_task = asyncio.create_task(self.sync_filters())
        sync_trends_task = asyncio.create_task(self.sync_trends())
        await self.sync_txios(blocks_added)
        channel_stats = await self.sync_claims(blocks_added)
        await self.sync_supports(blocks_added)
        if channel_stats:
            await self.sync_channel_stats(blocks_added, *channel_stats)
        await sync_trends_task
        await sync_filters_task
        if blocks_added:
            await self._on_block_controller.add(BlockEvent(blocks_added[-1]))

    async def advance_loop(self):
        while True:
            await self.advance_loop_event.wait()
            self.advance_loop_event.clear()
            try:
                await self.advance()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.exception(e)
                await self.stop()
