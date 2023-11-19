import asyncio

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

import json
import shlex
import argparse
import traceback
from uuid import uuid4
from typing import AsyncIterator

from aiohttp import web, WSMsgType


class Server:
    def __init__(self,
                 host: str='0.0.0.0',
                 port=5000,
                 timeout: float=90.0,
                 candle_path: str | None=None,
                 llama_cpp_path: str | None=None,
                 gguf_models_path: str | None=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.candle_path = candle_path
        self.llama_cpp_path = llama_cpp_path
        self.gguf_models_path = gguf_models_path
        self.app = web.Application()
        self.lock = asyncio.Lock()


    def _format_llama_cpp_cmd(self, kind: str, **kwargs) -> str:
        cmd: list[str] | str = []
        
        if kind == 'main':
            prompt: str = kwargs['prompt']
            model: str = kwargs['model']
            n_predict: int = int(kwargs.get('n_predict', '-1'))
            ctx_size: int = int(kwargs.get('ctx_size', '2048'))
            batch_size: int = int(kwargs.get('batch_size', '512'))
            temp: float = float(kwargs.get('temp', '0.8'))
            n_gpu_layers: int = int(kwargs.get('n_gpu_layers', '0'))
            top_k: int = int(kwargs.get('top_k', '40'))
            top_p: float = float(kwargs.get('top_p', '0.9'))
            shell_prompt: str = shlex.quote(prompt)

            cmd.extend([
                f'{self.llama_cpp_path}/main',
                '--model', f'{self.gguf_models_path}/{model}',
                '--n-predict', n_predict,
                '--ctx-size', ctx_size,
                '--batch-size', batch_size,
                '--temp', temp,
                '--n-gpu-layers', n_gpu_layers,
                '--top-k', top_k,
                '--top-p', top_p,
                # '--mlock',
                # '--no-mmap',
                '--simple-io',
                '--log-disable',
                '--prompt', shell_prompt,
            ])
        else:
            raise ValueError(f'Unsupported kind: {kind}')

        cmd = [str(n) for n in cmd]
        cmd = ' '.join(cmd)
        return cmd


    def _format_candle_cmd(self, kind: str, **kwargs) -> str:
        cmd: list[str] | str = []
        
        if kind == 'phi':
            prompt: str = kwargs['prompt']
            model: str = kwargs['model']
            temperature: int = float(kwargs.get('temperature', '0.8'))
            top_p: int = float(kwargs.get('top_p', '0.9'))
            sample_len: int = int(kwargs.get('sample_len', '100'))
            shell_prompt: str = shlex.quote(prompt)
            assert model in ('1', '1.5', 'puffin-phi-v2', 'phi-hermes')

            cmd.extend([
                f'{self.candle_path}/target/release/examples/phi',
                '--model', model,
                '--temperature', temperature,
                '--top-p', top_p,
                '--sample-len', sample_len,
                '--quantized',
                '--prompt', shell_prompt,
            ])
        elif kind == 'stable-lm':
            prompt: str = kwargs['prompt']
            model_id: str = kwargs['model_id']
            temperature: int = float(kwargs.get('temperature', '0.8'))
            top_p: int = float(kwargs.get('top_p', '0.9'))
            sample_len: int = int(kwargs.get('sample_len', '100'))
            shell_prompt: str = shlex.quote(prompt)
            assert model_id in ('lmz/candle-stablelm-3b-4e1t',)

            cmd.extend([
                f'{self.candle_path}/target/release/examples/stable-lm',
                '--model-id', model_id,
                '--temperature', temperature,
                '--top-p', top_p,
                '--sample-len', sample_len,
                '--quantized',
                '--use-flash-attn',
                '--prompt', shell_prompt,
            ])
        elif kind == 'llama':
            prompt: str = kwargs['prompt']
            model_id: str = kwargs.get('model_id')
            temperature: int = float(kwargs.get('temperature', '0.8'))
            top_p: int = float(kwargs.get('top_p', '0.9'))
            sample_len: int = int(kwargs.get('sample_len', '100'))
            shell_prompt: str = shlex.quote(prompt)

            cmd.extend([
                f'{self.candle_path}/target/release/examples/stable-lm',
            ])

            if model_id:
                cmd.extend([
                    '--model-id', model_id
                ])

            cmd.extend([
                '--temperature', temperature,
                '--top-p', top_p,
                '--sample-len', sample_len,
                '--quantized',
                '--use-flash-attn',
                '--prompt', shell_prompt,
            ])
        elif kind == 'quantized':
            prompt: str = kwargs['prompt']
            model: str = kwargs['model']
            temperature: int = float(kwargs.get('temperature', '0.8'))
            top_p: int = float(kwargs.get('top_p', '0.9'))
            sample_len: int = int(kwargs.get('sample_len', '100'))
            shell_prompt: str = shlex.quote(prompt)

            cmd.extend([
                f'{self.candle_path}/target/release/examples/quantized',
                '--model', f'{self.gguf_models_path}/{model}',
                '--temperature', temperature,
                '--top-p', top_p,
                '--sample-len', sample_len,
                '--prompt', shell_prompt,
            ])
        else:
            raise ValueError(f'Unsupported kind: {kind}')

        cmd = [str(n) for n in cmd]
        cmd = ' '.join(cmd)
        return cmd


    def _format_cmd(self, msg: dict):
        engine: str = msg['engine']
        cmd: str

        if engine == 'llama.cpp':
            cmd = self._format_llama_cpp_cmd(**msg)
        elif engine == 'candle':
            cmd = self._format_candle_cmd(**msg)
        else:
            raise ValueError(f'Unknown engine: {engine}')

        return cmd


    async def _run_shell_cmd(self, msg: dict, cmd: str) -> AsyncIterator[str]:
        prompt: str = msg['prompt']
        stop: str = msg.get('stop', [])
        prompt_enc: bytes = prompt.encode()
        shell_prompt: str = shlex.quote(prompt)
        stop_enc = None if stop is None else [n.encode() for n in stop]
        stdout: bytes = b''

        print(f'[DEBUG] _run_shell_cmd: {cmd}')

        try:
            async with asyncio.timeout(self.timeout):
                # create new proc for model
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # receive original prompt in stdout
                # strip original prompt from return
                prev_buf: bytes
                buf: bytes
                text: str

                while not proc.stdout.at_eof():
                    # stdout
                    buf = await proc.stdout.read(1024)
                    stdout += buf

                    # skip original prompt
                    if len(stdout) > len(prompt_enc):
                        break

                    await asyncio.sleep(0.2)

                # yield left-overs from stdout as buf
                stdout = stdout[len(prompt_enc):]
                buf = stdout
                prev_buf = b''
                text = stdout.decode()
                # yield text

                # read rest of tokens
                stopped: bool = False

                while not proc.stdout.at_eof():
                    buf = await proc.stdout.read(256)
                    prev_buf += buf
                    stdout += buf

                    try:
                        text = prev_buf.decode()
                    except Exception as e:
                        print(f'[ERROR] buf.decode() exception: {e}')
                        continue

                    # NOTE: candle, check 'loaded XYZ tensors' and 'model built\n' in first line and strip it
                    if 'loaded' in text and 'tensors' in text and text.endswith('\n'):
                        text = ''
                    elif 'model built\n' in text:
                        text = ''

                    # NOTE: candle, check 'tokens generated' in last line and strip it
                    candle_eos = 'tokens generated ('
                    candle_eos_1 = 'token/s'

                    if candle_eos in text and candle_eos_2 in text:
                        text = text[:text.index(candle_eos)]
                        text = text[:text.rindex('\n')]

                    prev_buf = b''
                    yield text

                    # check for stop words
                    if stop_enc:
                        for n in stop_enc:
                            if n in stdout:
                                print(f'[INFO] stop word: {stop!r}')
                                stdout = stdout[:stdout.index(n)]
                                stopped = True
                                break

                    if stopped:
                        break

                    await asyncio.sleep(0.2)

                if stopped:
                    print(f'[INFO] stop word, trying to kill proc: {proc}')

                    try:
                        proc.kill()
                        await proc.wait()
                        print('[INFO] proc kill [stop]')
                    except Exception as e:
                        print(f'[INFO] proc kill [stop]: {e}')
                    finally:
                        proc = None
                
                # read stderr at once
                stderr = await proc.stderr.read()
        except asyncio.TimeoutError as e:
            print(f'[ERROR] timeout, trying to kill proc: {proc}')

            try:
                proc.kill()
                await proc.wait()
                print('[INFO] proc kill [timeout]')
            except Exception as e:
                print(f'[INFO] proc kill [timeout]: {e}')
                raise e
            finally:
                proc = None


    def _run_cmd(self, msg: dict) -> AsyncIterator[str]:
        engine: str = msg['engine']
        cmd: str = self._format_cmd(msg)
        res: AsyncIterator[str]

        if engine in ('llama.cpp', 'candle'):
            res = self._run_shell_cmd(msg, cmd)
        else:
            raise ValueError(f'Unknown engine: {engine}')

        return res


    async def _api_1_0_chat_completions(self, ws: web.WebSocketResponse, msg: dict):
        async for chunk in self._run_cmd(msg):
            print(f'chunk: {chunk!r}')
            msg: dict = {'chunk': chunk}
            await ws.send_json(msg)

        await ws.close()


    async def post_api_1_0_text_completions(self, request):
        raise NotImplementedError


    async def post_api_1_0_chat_completions(self, request):
        raise NotImplementedError


    async def get_api_1_0_text_completions(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        print(f'[INFO] websocket openned: {ws}')
        
        try:
            async with asyncio.TaskGroup() as tg:
                async for msg in ws:
                    if msg.type == WSMsgType.PING:
                        await ws.pong(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        if msg.data == 'close':
                            await ws.close()
                        
                        data = json.loads(msg.data)
                        coro = self._api_1_0_chat_completions(ws, data)
                        task = tg.create_task(coro)
                    elif msg.type == WSMsgType.ERROR:
                        print(f'[ERROR] websocket closed with exception: {ws.exception()}')
        except ExceptionGroup as e:
            traceback.print_exc()
            print(f'[ERROR] websocket ExceptionGroup: {e}')

            # close ws
            await ws.close()

        print(f'[INFO] websocket closed: {ws}')
        return ws


    async def get_api_1_0_chat_completions(self, request):
        raise NotImplementedError


    def run(self):
        self.app.add_routes([
            web.post('/api/1.0/text/completions', self.post_api_1_0_text_completions),
            web.post('/api/1.0/chat/completions', self.post_api_1_0_chat_completions),
            web.get('/api/1.0/text/completions', self.get_api_1_0_text_completions),
            web.get('/api/1.0/chat/completions', self.get_api_1_0_chat_completions),
        ])

        web.run_app(self.app, host=self.host, port=self.port)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='server', description='Python llama.cpp HTTP Server')
    parser.add_argument('--host', help='http server host', default='0.0.0.0')
    parser.add_argument('--port', help='http server port', default=5000, type=float)
    parser.add_argument('--timeout', help='llama.cpp timeout in seconds', default=300.0, type=float)
    parser.add_argument('--candle-path', help='candle directory path', default='~/candle')
    parser.add_argument('--llama-cpp-path', help='llama.cpp directory path', default='~/llama.cpp')
    parser.add_argument('--gguf-models-path', help='gguf models directory path', default='~/models')
    cli_args = parser.parse_args()

    server = Server(
        host=cli_args.host,
        port=cli_args.port,
        timeout=cli_args.timeout,
        candle_path=cli_args.candle_path,
        llama_cpp_path=cli_args.llama_cpp_path,
        gguf_models_path=cli_args.gguf_models_path,
    )

    server.run()
