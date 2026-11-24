"""Sandboxed code execution with resource limits and isolation."""
from __future__ import annotations

import ast
import sys
import time
import traceback
from io import StringIO
from pathlib import Path
from typing import Any
from .resource_limiter import ResourceLimiter


class SandboxResult:
    """Result of sandboxed code execution."""
    
    def __init__(
        self,
        success: bool,
        output: str = "",
        error: str = "",
        duration: float = 0.0,
        return_value: Any = None
    ):
        self.success = success
        self.output = output
        self.error = error
        self.duration = duration
        self.return_value = return_value
    
    def to_dict(self) -> dict:
        return {
            'success': self.success,
            'output': self.output,
            'error': self.error,
            'duration': self.duration,
            'has_return': self.return_value is not None
        }


class Sandbox:
    """Sandboxed Python code execution."""
    
    def __init__(
        self,
        timeout: float = 5.0,
        memory_mb: int = 256,
        limiter: ResourceLimiter | None = None
    ):
        self.timeout = timeout
        self.memory_mb = memory_mb
        self.limiter = limiter or ResourceLimiter(memory_mb=memory_mb)
    
    def execute(
        self,
        code: str,
        globals_dict: dict | None = None,
        test_cases: list[dict] | None = None
    ) -> SandboxResult:
        """Execute code in sandbox with timeout and resource limits."""
        start_time = time.time()
        
        # Capture stdout/stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdout_capture = StringIO()
        stderr_capture = StringIO()
        
        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture
            
            # Set up execution environment
            exec_globals = globals_dict.copy() if globals_dict else {}
            exec_globals['__builtins__'] = __builtins__
            
            # Parse and check for dangerous operations
            try:
                tree = ast.parse(code)
                self._check_safety(tree)
            except SyntaxError as e:
                return SandboxResult(
                    success=False,
                    error=f"Syntax error: {e}",
                    duration=time.time() - start_time
                )
            
            # Execute with timeout
            try:
                with self.limiter.limit():
                    exec(compile(tree, '<sandbox>', 'exec'), exec_globals)
                
                output = stdout_capture.getvalue()
                
                # Run test cases if provided
                if test_cases:
                    test_result = self._run_tests(code, test_cases, exec_globals)
                    if not test_result['success']:
                        return SandboxResult(
                            success=False,
                            output=output,
                            error=test_result['error'],
                            duration=time.time() - start_time
                        )
                
                return SandboxResult(
                    success=True,
                    output=output,
                    duration=time.time() - start_time
                )
                
            except TimeoutError:
                return SandboxResult(
                    success=False,
                    error=f"Timeout after {self.timeout}s",
                    duration=time.time() - start_time
                )
                
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
            return SandboxResult(
                success=False,
                output=stdout_capture.getvalue(),
                error=error_msg,
                duration=time.time() - start_time
            )
            
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
    
    def _check_safety(self, tree: ast.Module) -> None:
        """Check code for dangerous operations."""
        dangerous_modules = {'os', 'subprocess', 'shutil', 'sys', 'ctypes'}
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] in dangerous_modules:
                        raise ValueError(f"Dangerous import: {alias.name}")
            
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] in dangerous_modules:
                    raise ValueError(f"Dangerous import from: {node.module}")
            
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in {'exec', 'eval', 'compile'}:
                        raise ValueError(f"Dangerous function: {node.func.id}")
    
    def _run_tests(
        self,
        code: str,
        test_cases: list[dict],
        exec_globals: dict
    ) -> dict:
        """Run test cases against executed code."""
        for i, test in enumerate(test_cases):
            test_code = test.get('code', '')
            expected = test.get('expected')
            
            try:
                test_globals = exec_globals.copy()
                exec(test_code, test_globals)
                
                if expected is not None:
                    # Simple comparison for now
                    # In production, use more sophisticated comparison
                    result_str = str(test_globals.get('__result__'))
                    if result_str != str(expected):
                        return {
                            'success': False,
                            'error': f"Test {i+1} failed: expected {expected}, got {result_str}"
                        }
                        
            except Exception as e:
                return {
                    'success': False,
                    'error': f"Test {i+1} error: {type(e).__name__}: {str(e)}"
                }
        
        return {'success': True}


def create_sandbox(
    timeout: float = 5.0,
    memory_mb: int = 256
) -> Sandbox:
    """Factory function to create a sandbox."""
    return Sandbox(timeout=timeout, memory_mb=memory_mb)


if __name__ == '__main__':
    # Test the sandbox
    sandbox = Sandbox(timeout=5.0, memory_mb=256)
    
    # Test 1: Simple code
    result = sandbox.execute("""
def add(a, b):
    return a + b

print(add(2, 3))
""")
    print(f"Test 1: success={result.success}, output={result.output}")
    
    # Test 2: Error case
    result = sandbox.execute("""
def divide(a, b):
    return a / b

print(divide(10, 0))
""")
    print(f"Test 2: success={result.success}, error={result.error}")
    
    # Test 3: Dangerous code
    result = sandbox.execute("""
import os
os.system('echo hello')
""")
    print(f"Test 3: success={result.success}, error={result.error}")