# Copyright 1999-2020 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

from mars.utils import get_next_port
from mars.oscar import Actor
from mars.oscar.context import get_context
from mars.oscar.backends.mars import create_actor_pool
from mars.oscar.backends.mars.allocate_strategy import \
    AddressSpecified, IdleLabel, MainPool, RandomSubPool
from mars.oscar.backends.mars.config import ActorPoolConfig
from mars.oscar.backends.mars.message import new_message_id, \
    CreateActorMessage, DestroyActorMessage, HasActorMessage, \
    ActorRefMessage, SendMessage, TellMessage, ControlMessage, \
    ControlMessageType, MessageType
from mars.oscar.backends.mars.pool import SubActorPool, MainActorPool
from mars.oscar.backends.mars.router import Router
from mars.oscar.errors import NoIdleSlot, ActorNotExist
from mars.oscar.utils import create_actor_ref
from mars.tests.core import mock


# test create actor

class _CannotBeUnpickled:
    def __getstate__(self):
        return ()

    def __setstate__(self, state):
        raise RuntimeError("cannot unpickle")


class TestActor(Actor):
    __test__ = False

    def __init__(self):
        self.value = 0

    def add(self, val):
        self.value += val
        return self.value

    def return_cannot_unpickle(self):
        return _CannotBeUnpickled()


@pytest.mark.asyncio
@mock.patch('mars.oscar.backends.mars.pool.SubActorPool.notify_main_pool_to_destroy')
async def test_sub_actor_pool(notify_main_pool):
    notify_main_pool.return_value = None

    config = ActorPoolConfig()
    config.add_pool_conf(0, 'main', 'unixsocket:///0', f'127.0.0.1:{get_next_port()}')
    config.add_pool_conf(1, 'sub', 'unixsocket:///1', f'127.0.0.1:{get_next_port()}')

    pool = await SubActorPool.create({
        'actor_pool_config': config,
        'process_index': 1
    })
    await pool.start()

    create_actor_message = CreateActorMessage(
        new_message_id(),
        TestActor, b'test', tuple(), dict(),
        AddressSpecified(pool.external_address))
    message = await pool.create_actor(create_actor_message)
    assert message.message_type == MessageType.result
    actor_ref = message.result
    assert actor_ref.address == pool.external_address
    assert actor_ref.uid == b'test'

    has_actor_message = HasActorMessage(
        new_message_id(), actor_ref)
    assert (await pool.has_actor(has_actor_message)).result is True

    actor_ref_message = ActorRefMessage(
        new_message_id(), actor_ref)
    assert (await pool.actor_ref(actor_ref_message)).result == actor_ref

    tell_message = TellMessage(
        new_message_id(), actor_ref, ('add', 1, dict()))
    message = await pool.tell(tell_message)
    assert message.result is None

    send_message = SendMessage(
        new_message_id(), actor_ref, ('add', 3, dict()))
    message = await pool.send(send_message)
    assert message.result == 4

    # test error message
    # type mismatch
    send_message = SendMessage(
        new_message_id(), actor_ref, ('add', '3', dict()))
    result = await pool.send(send_message)
    assert result.message_type == MessageType.error
    assert isinstance(result.error, TypeError)

    send_message = SendMessage(
        new_message_id(), create_actor_ref(actor_ref.address, 'non_exist'),
        ('add', 3, dict()))
    result = await pool.send(send_message)
    assert isinstance(result.error, ActorNotExist)

    # test processing message on background
    async with await pool.router.get_client(pool.external_address) as client:
        send_message = SendMessage(
            new_message_id(), actor_ref, ('add', 5, dict()))
        await client.send(send_message)
        result = await client.recv()
        assert result.result == 9

        send_message = SendMessage(
            new_message_id(), actor_ref, ('add', '5', dict()))
        await client.send(send_message)
        result = await client.recv()
        assert isinstance(result.error, TypeError)

    destroy_actor_message = DestroyActorMessage(
        new_message_id(), actor_ref)
    message = await pool.destroy_actor(destroy_actor_message)
    assert message.result == actor_ref.uid

    # send destroy failed
    message = await pool.destroy_actor(destroy_actor_message)
    assert isinstance(message.error, ActorNotExist)

    message = await pool.has_actor(has_actor_message)
    assert not message.result

    # test sync config
    config.add_pool_conf(1, 'sub', 'unixsocket:///1', f'127.0.0.1:{get_next_port()}')
    sync_config_message = ControlMessage(
        new_message_id(), ControlMessageType.sync_config, config)
    message = await pool.handle_control_command(sync_config_message)
    assert message.result is True

    # test get config
    get_config_message = ControlMessage(
        new_message_id(), ControlMessageType.get_config, None)
    message = await pool.handle_control_command(get_config_message)
    config2 = message.result
    assert config.as_dict() == config2.as_dict()

    stop_message = ControlMessage(
        new_message_id(), ControlMessageType.stop, None)
    message = await pool.handle_control_command(stop_message)
    assert message.result is True

    assert pool.router is Router.get_instance()
    assert pool.external_address == pool.router.external_address

    await pool.join(.05)
    assert pool.stopped


@pytest.mark.asyncio
async def test_main_actor_pool():
    config = ActorPoolConfig()
    my_label = 'computation'
    main_address = f'127.0.0.1:{get_next_port()}'
    config.add_pool_conf(0, 'main', 'unixsocket:///0', main_address)
    config.add_pool_conf(1, my_label, 'unixsocket:///1', f'127.0.0.1:{get_next_port()}',
                         env={'my_env': '1'})
    config.add_pool_conf(2, my_label, 'unixsocket:///2', f'127.0.0.1:{get_next_port()}')

    strategy = IdleLabel(my_label, 'my_test')

    async with await MainActorPool.create({'actor_pool_config': config}) as pool:
        create_actor_message = CreateActorMessage(
            new_message_id(), TestActor, b'test', tuple(), dict(), MainPool())
        message = await pool.create_actor(create_actor_message)
        actor_ref = message.result
        assert actor_ref.address == main_address

        create_actor_message1 = CreateActorMessage(
            new_message_id(), TestActor, b'test1', tuple(), dict(), strategy)
        message1 = await pool.create_actor(create_actor_message1)
        actor_ref1 = message1.result
        assert actor_ref1.address in config.get_external_addresses(my_label)

        create_actor_message2 = CreateActorMessage(
            new_message_id(), TestActor, b'test2', tuple(), dict(), strategy)
        message2 = await pool.create_actor(create_actor_message2)
        actor_ref2 = message2.result
        assert actor_ref2.address in config.get_external_addresses(my_label)
        assert actor_ref2.address != actor_ref1.address

        create_actor_message3 = CreateActorMessage(
            new_message_id(), TestActor, b'test3', tuple(), dict(), strategy)
        message3 = await pool.create_actor(create_actor_message3)
        # no slot to allocate the same label
        assert isinstance(message3.error, NoIdleSlot)

        has_actor_message = HasActorMessage(
            new_message_id(), create_actor_ref(main_address, b'test2'))
        assert (await pool.has_actor(has_actor_message)).result is True

        actor_ref_message = ActorRefMessage(
            new_message_id(), create_actor_ref(main_address, b'test2'))
        assert (await pool.actor_ref(actor_ref_message)).result == actor_ref2

        # tell
        tell_message = TellMessage(
            new_message_id(), actor_ref1, ('add', 2, dict()))
        message = await pool.tell(tell_message)
        assert message.result is None

        # send
        send_message = SendMessage(
            new_message_id(), actor_ref1, ('add', 4, dict()))
        assert (await pool.send(send_message)).result == 6

        # test error message
        # type mismatch
        send_message = SendMessage(
            new_message_id(), actor_ref1, ('add', '3', dict()))
        result = await pool.send(send_message)
        assert isinstance(result.error, TypeError)

        # send and tell to main process
        tell_message = TellMessage(
            new_message_id(), actor_ref, ('add', 2, dict()))
        message = await pool.tell(tell_message)
        assert message.result is None
        send_message = SendMessage(
            new_message_id(), actor_ref, ('add', 4, dict()))
        assert (await pool.send(send_message)).result == 6

        # destroy
        destroy_actor_message = DestroyActorMessage(
            new_message_id(), actor_ref1)
        message = await pool.destroy_actor(destroy_actor_message)
        assert message.result == actor_ref1.uid

        # destroy via connecting to sub pool directly
        async with await pool.router.get_client(
                config.get_external_addresses()[-1]) as client:
            destroy_actor_message = DestroyActorMessage(
                new_message_id(), actor_ref2)
            await client.send(destroy_actor_message)
            result = await client.recv()
            assert result.result == actor_ref2.uid

    assert pool.stopped


@pytest.mark.asyncio
async def test_create_actor_pool():
    pool = await create_actor_pool('127.0.0.1', n_process=2)

    async with pool:
        ctx = get_context()

        # actor on main pool
        actor_ref = await ctx.create_actor(TestActor, uid='test-1',
                                           address=pool.external_address)
        assert await actor_ref.add(3) == 3
        assert await actor_ref.add(1) == 4
        assert (await ctx.has_actor(actor_ref)) is True
        assert (await ctx.actor_ref(actor_ref)) == actor_ref
        await ctx.destroy_actor(actor_ref)
        assert (await ctx.has_actor(actor_ref)) is False

        # actor on sub pool
        actor_ref2 = await ctx.create_actor(TestActor, uid='test-2',
                                            address=pool.external_address,
                                            allocate_strategy=RandomSubPool())
        assert actor_ref2.address != actor_ref.address
        assert await actor_ref2.add(3) == 3
        assert await actor_ref2.add(1) == 4
        with pytest.raises(RuntimeError):
            await actor_ref2.return_cannot_unpickle()
        assert (await ctx.has_actor(actor_ref2)) is True
        assert (await ctx.actor_ref(actor_ref2)) == actor_ref2
        await ctx.destroy_actor(actor_ref2)
        assert (await ctx.has_actor(actor_ref2)) is False


@pytest.mark.asyncio
async def test_errors():
    with pytest.raises(ValueError):
        _ = await create_actor_pool('127.0.0.1', n_process=1, labels=['a'])

    with pytest.raises(ValueError):
        _ = await create_actor_pool(f'127.0.0.1:{get_next_port()}', n_process=1,
                                    ports=[get_next_port(), get_next_port()])

    with pytest.raises(ValueError):
        _ = await create_actor_pool('127.0.0.1', n_process=1,
                                    ports=[get_next_port()])
