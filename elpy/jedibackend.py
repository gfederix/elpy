"""Elpy backend using the Jedi library.

This backend uses the Jedi library:

https://github.com/davidhalter/jedi

"""
from __future__ import annotations

import sys
import traceback
import re

from array import array
from io import StringIO
from bisect import bisect_right

from typing import List, Optional, Union, NamedTuple, Any, NoReturn
from pathlib import Path
import jedi


from elpy import rpc
from elpy.rpc import Fault, Result

class NameResult(Result):
    name: str
    offset: int
    filename: str

    @classmethod
    def from_name(
            cls, x: jedi.api.classes.Name, offset: int) -> NameResult:
        if type(x) is not jedi.api.classes.Name:
            raise TypeError("Must be jedi.api.classes.Name")
        return cls(name=x.name, filename=str(x.module_path), offset=offset)


class RefactoringResult(Result):
    success: bool
    project_path: Path
    diff: str
    changed_files: List[Path]
    error_msg: Optional[str]

    @classmethod
    def fail(cls, error_msg=""):
        return cls(
            success=False, project_path=Path(), diff="", changed_files=[],
            error_msg=error_msg)

    @classmethod
    def from_refactoring(
            cls, x: jedi.api.refactoring.Refactoring) -> RefactoringResult:
        if type(x) is not jedi.api.refactoring.Refactoring:
            raise TypeError("Must be jedi.api.refactoring.Refactoring type")
        return cls(
            success=True,
            project_path=x._inference_state.project._path,
            diff=x.get_diff(),
            changed_files=list(x.get_changed_files().keys()))


# in case pkg_resources is not properly installed
# (see https://github.com/jorgenschaefer/elpy/issues/1674).

try:
    from pkg_resources import parse_version
except ImportError:  # pragma: no cover
    def parse_version(*arg, **kwargs) -> NoReturn:
        raise Fault("`pkg_resources` could not be imported, "
                    "please reinstall Elpy RPC virtualenv with"
                    " `M-x elpy-rpc-reinstall-virtualenv`", code=400)

class JediBackend(object):
    """The Jedi backend class.

    Implements the RPC calls we can pass on to Jedi.

    Documentation: http://jedi.jedidjah.ch/en/latest/docs/plugin-api.html

    """
    name = "jedi"

    def __init__(self, project_root, environment_binaries_path):
            
        self.project_root = project_root
        self.environment = None
        if environment_binaries_path is not None:
            try:
                self.environment = jedi.create_environment(
                    environment_binaries_path, safe=False)
            except jedi.api.environment.InvalidPythonEnvironment as e:
                Fault(message=str(e))
        self.completions = {}
        sys.path.append(project_root)
    
    def rpc_get_completions(self, filename, source, offset):
        src = SourceCode(filename, source)
        line, column = src.get_pos(offset)
        proposals = run_with_debug(jedi, 'complete', code=str(src),
                                   path=filename,
                                   environment=self.environment,
                                   fun_kwargs={'line': line, 'column': column})
        self.completions = dict((proposal.name, proposal)
                                for proposal in proposals)
        return [{'name': proposal.name.rstrip("="),
                 'suffix': proposal.complete.rstrip("="),
                 'annotation': proposal.type,
                 'meta': proposal.description}
                for proposal in proposals]

    def rpc_get_completion_docstring(self, completion) -> Optional[Any]:
        proposal = self.completions.get(completion)
        if proposal is not None:
            return proposal.docstring(fast=False)

    def rpc_get_completion_location(self, completion):
        proposal = self.completions.get(completion)
        if proposal is not None:
            return (str(proposal.module_path), proposal.line)

    def rpc_get_docstring(self, filename: str, source: str, offset: int
                          ) -> Optional[Any]:
        src = SourceCode(filename, source)
        line, column = src.get_pos(offset)
        locations = run_with_debug(jedi, 'goto',
                                   code=source,
                                   path=filename,
                                   environment=self.environment,
                                   fun_kwargs={'line': line,
                                               'column': column,
                                               'follow_imports': True,
                                               'follow_builtin_imports': True})
        # Filter uninteresting things
        uninternsting_to_be_filtered_names = {
            "str", "int", "float", "bool", "tuple", "list", "dict"}
        if not locations \
           or locations[-1].name in uninternsting_to_be_filtered_names \
           or not locations[-1].docstring():
            return None
        return ('Documentation for {0}:\n\n'.format(
            locations[-1].full_name) + locations[-1].docstring())

    def rpc_get_definition(self, filename, source, offset):
        line, column = pos_to_linecol(source, offset)
        src = SourceCode(filename, source)
        line, column = src.get_pos(offset)
        locations = run_with_debug(jedi, 'goto',
                                   code=source,
                                   path=filename,
                                   environment=self.environment,
                                   fun_kwargs={'line': line,
                                               'column': column,
                                               'follow_imports': True,
                                               'follow_builtin_imports': True})

        if not locations:
            return None
        # goto_definitions() can return silly stuff like __builtin__
        # for int variables, so we remove them. See issue #76.
        locations = [
            loc for loc in locations
            if (loc.module_path is not None
                and loc.module_name != 'builtins'
                and loc.module_name != '__builtin__')]
        if len(locations) == 0:
            return None
        loc = locations[-1]
        try:
            if loc.module_path == Path(filename):
                offset = src.get_offset(loc.line, loc.column)
            else:
                offset = SourceCode(
                    path=loc.module_path).get_offset(loc.line, loc.column)
        except IOError:  # pragma: no cover
            return None
        return (str(loc.module_path), offset)


    def rpc_get_assignment(self, filename, source, offset):
        raise Fault("Obsolete since jedi 17.0. Please use 'get_definition'.")


    def rpc_get_calltip(self, filename, source, offset):
        line, column = pos_to_linecol(source, offset)
        calls = run_with_debug(jedi, 'get_signatures',
                               code=source,
                               path=filename,
                               environment=self.environment,
                               fun_kwargs={'line': line,
                                           'column': column})
        if not calls:
            return None
        params = [re.sub("^param ", '', param.description)
                  for param in calls[0].params]
        return {"name": calls[0].name,
                "index": calls[0].index,
                "params": params}

    def rpc_get_calltip_or_oneline_docstring(self, filename, source, offset):
        """
        Return the current function calltip or its oneline docstring.

        Meant to be used with eldoc.
        """
        # Try to get a oneline docstring then
        docs = self.rpc_get_oneline_docstring(filename=filename,
                                              source=source,
                                              offset=offset)
        if docs is not None:
            if docs['doc'] != "No documentation":
                docs['kind'] = 'oneline_doc'
                return docs
        # Try to get a calltip
        calltip = self.rpc_get_calltip(filename=filename, source=source,
                                       offset=offset)
        if calltip is not None:
            calltip['kind'] = 'calltip'
            return calltip
        # Ok, no calltip, just display the function name
        if docs is not None:
            docs['kind'] = 'oneline_doc'
            return docs
        # Giving up...
        return None

    def rpc_get_oneline_docstring(self, filename, source, offset):
        """Return a oneline docstring for the symbol at offset"""
        src = SourceCode(filename, source)
        line, column = src.get_pos(offset)
        definitions = run_with_debug(jedi, 'goto',
                                     code=str(src),
                                     path=filename,
                                     environment=self.environment,
                                     fun_kwargs={'line': line,
                                                 'column': column})
        if not definitions:
            return None
        # avoid unintersting stuff
        definitions = [defi for defi in definitions
                       if defi.name not in
                       ["str", "int", "float", "bool", "tuple",
                        "list", "dict"]]
        if len(definitions) == 0:
            return None
        definition = definitions[0]
        # Get name
        if definition.type in ['function', 'class']:
            raw_name = definition.name
            name = '{}()'.format(raw_name)
            doc = definition.docstring().split('\n')
        elif definition.type in ['module']:
            raw_name = definition.name
            name = '{} {}'.format(raw_name, definition.type)
            doc = definition.docstring().split('\n')
        elif (definition.type in ['instance']
              and hasattr(definition, "name")):
            raw_name = definition.name
            name = raw_name
            doc = definition.docstring().split('\n')
        else:
            return None
        # Keep only the first paragraph that is not a function declaration
        lines = []
        call = "{}(".format(raw_name)
        # last line
        doc.append('')
        for i in range(len(doc)):
            if doc[i] == '' and len(lines) != 0:
                paragraph = " ".join(lines)
                lines = []
                if call != paragraph[0:len(call)]:
                    break
                paragraph = ""
                continue
            lines.append(doc[i])
        # Keep only the first sentence
        onelinedoc = paragraph.split('. ', 1)
        if len(onelinedoc) == 2:
            onelinedoc = onelinedoc[0] + '.'
        else:
            onelinedoc = onelinedoc[0]
        if onelinedoc == '':
            onelinedoc = "No documentation"
        return {"name": name,
                "doc": onelinedoc}

    def rpc_get_usages(self, filename, source, offset) -> List[NameResult]:
        """Return the uses of the symbol at offset.

        Returns a list of occurrences of the symbol, as dicts with the
        fields name, filename, and offset.

        """
        src = SourceCode(filename, source)
        line, column = src.get_pos(offset)
        names = run_with_debug(jedi, 'get_references',
                              code=str(src),
                              path=filename,
                              environment=self.environment,
                              fun_kwargs={'line': line,
                                          'column': column})
        result = []
        for name in names:
            if name.module_path == Path(filename):
                offset = src.get_offset(name.line, name.column)
            elif name.module_path is not None:
                other_src = SourceCode(name.module_path)
                offset = other_src.get_offset(name.line, name.column)
            result.append(NameResult.from_name(name, offset))
        return result

    def rpc_get_names(self, filename, source, offset) -> List[Result]:
        """Return the list of possible names"""
        src = SourceCode(filename, source)
        names = run_with_debug(jedi, 'get_names',
                               code=str(src),
                               path=filename,
                               environment=self.environment,
                               fun_kwargs={'all_scopes': True,
                                           'definitions': True,
                                           'references': True})
        result = []
        for name in names:
            if name.module_path == src.path:
                offset = src.get_offset(name.line, name.column)
            elif name.module_path is not None:
                other_src = SourceCode(name.module_path)
                offset = other_src.get_offset(name.line, name.column)
            result.append(NameResult.from_name(name, offset))
        return result
        
    def rpc_get_rename_diff(self, filename, source, offset, new_name):
        """Get the diff resulting from renaming the thing at point"""
        src = SourceCode(filename, source)
        line, column = src.get_pos(offset)
        script = jedi.Script(code=str(src), path=filename,
                             environment=self.environment)
        try:
            ref = script.rename(line=line,
                                column=column,
                                new_name=new_name)
        except Exception as e:
            return RefactoringResult.fail(error_msg=str(e))
        return RefactoringResult.from_refactoring(ref)

    def rpc_get_extract_variable_diff(
            self, filename, source, offset, new_name,
            line_beg, line_end, col_beg, col_end) -> Result:
        """Get the diff resulting from extracting the selected code"""
        ref = run_with_debug(jedi, 'extract_variable', code=source,
                             path=filename,
                             environment=self.environment,
                             fun_kwargs={'line': line_beg,
                                         'until_line': line_end,
                                         'column': col_beg,
                                         'until_column': col_end,
                                         'new_name': new_name})
        if ref is None:
            return RefactoringResult.fail()
        else:
            return RefactoringResult.from_refactoring(ref)

    def rpc_get_extract_function_diff(
            self, filename, source, offset, new_name,
            line_beg, line_end, col_beg, col_end) -> Result:
        """Get the diff resulting from extracting the selected code"""
        script = jedi.Script(code=source, path=filename,
                             environment=self.environment)
        try:
            ref = script.extract_function(line=line_beg,
                                          until_line=line_end,
                                          column=col_beg,
                                          until_column=col_end,
                                          new_name=new_name)
        except Exception as e:
            return RefactoringResult.fail(error_msg=str(e))
        return RefactoringResult.from_refactoring(ref)

    def rpc_get_inline_diff(self, filename, source, offset) -> Result:
        """Get the diff resulting from inlining the selected variable"""
        src = SourceCode(filename, source)
        line, column = src.get_pos(offset)
        ref = run_with_debug(jedi, 'inline', code=str(src),
                             path=filename,
                             environment=self.environment,
                             fun_kwargs={'line': line,
                                         'column': column})
        if ref is None:
            return RefactoringResult.fail()
        else:
            return RefactoringResult.from_refactoring(ref)


# From the Jedi documentation:
#
#   line is the current line you want to perform actions on (starting
#   with line #1 as the first line). column represents the current
#   column/indent of the cursor (starting with zero). source_path
#   should be the path of your file in the file system.
class Pos(NamedTuple):
    line: int
    col: int

class SourceCode:
    _source: Optional[str]
    _path: Optional[Path]
    _line_offsets: Optional[array[int]]
    
    def __init__(
            self, path: Union[None, str, Path], source: Optional[str] = None,
            line_start: int = 1, col_start: int = 0):
        if path is not None:
            self._path = Path(path)
        self._source = source
        self._line_offsets = None
        self._line_start = line_start
        self._col_start = col_start
        
    def get_source(self):
        if self._source is None:
            with open(self._path, 'r') as fh:
                self._source = fh.read()
        return self._source

    def __str__(self):
        return self.get_source()

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def get_pos(self, offset: int) -> Pos:
        """Return a tuple of line and column for offset pos in text.

        Lines are one-based, columns zero-based.

        This is how Jedi wants it. Don't ask me why.
        
        """
        idx = self._get_line_offsets()
        line_num = bisect_right(idx, offset) - 1
        line_offset = idx[line_num]
        return Pos(line_num + self._line_start,
                   offset - line_offset + self._col_start)

    def get_offset(self, line: int, col: int):
        """Return the offset of this line and column in text.
        
        Lines are one-based, columns zero-based.
        
        This is how Jedi wants it. Don't ask me why.
        
        """
        line_offsets = self._get_line_offsets()
        line_from_zero = line - self._line_start
        col_from_zero = col - self._col_start
        try:
            cur_line_offset = line_offsets[line_from_zero]
        except IndexError:
            raise ValueError(f"Text does not have {line} lines.")
        offset =  cur_line_offset + col_from_zero
        next_line_offset = line_offsets[line_from_zero + 1]
        if not offset < next_line_offset:
            raise ValueError(
                f"Line {line} column {col} is not within the text")
        return offset
    
    def _get_line_offsets(self) -> array[int]:
        if self._line_offsets is None:
            self._line_offsets = array('I')
            self._line_offsets.append(0)
            curr_line_offset = 0
            for line in StringIO(self.get_source()):
                curr_line_offset += len(line)
                self._line_offsets.append(curr_line_offset)
        return self._line_offsets


def pos_to_linecol(text, pos):
    """Return a tuple of line and column for offset pos in text.

    Lines are one-based, columns zero-based.

    This is how Jedi wants it. Don't ask me why.

    """
    line_start = text.rfind("\n", 0, pos) + 1
    line = text.count("\n", 0, line_start) + 1
    col = pos - line_start
    return line, col


def run_with_debug(jedi, name, fun_kwargs={}, *args, **kwargs):
    re_raise = kwargs.pop('re_raise', ())
    try:
        script = jedi.Script(*args, **kwargs)
        return getattr(script, name)(**fun_kwargs)
    except Exception as e:
        if isinstance(e, re_raise):
            raise
        if isinstance(e, jedi.RefactoringError):
            return None
        # Bug jedi#485
        if (
                isinstance(e, ValueError) and
                "invalid \\x escape" in str(e)
        ):
            return None
        # Bug jedi#485 in Python 3
        if (
                isinstance(e, SyntaxError) and
                "truncated \\xXX escape" in str(e)
        ):
            return None

        from jedi import debug

        debug_info = []

        def _debug(level, str_out):
            if level == debug.NOTICE:
                prefix = "[N]"
            elif level == debug.WARNING:
                prefix = "[W]"
            else:
                prefix = "[?]"
            debug_info.append(u"{0} {1}".format(prefix, str_out))

        jedi.set_debug_function(_debug, speed=False)
        try:
            script = jedi.Script(*args, **kwargs)
            return getattr(script, name)()
        except Exception as e:
            source = kwargs.get('source')
            sc_args = []
            sc_args.extend(repr(arg) for arg in args)
            sc_args.extend("{0}={1}".format(k, "source" if k == "source"
                                            else repr(v))
                           for (k, v) in kwargs.items())

            data = {
                "traceback": traceback.format_exc(),
                "jedi_debug_info": {'script_args': ", ".join(sc_args),
                                    'source': source,
                                    'method': name,
                                    'debug_info': debug_info}
            }
            raise rpc.Fault(message=str(e),
                            code=500,
                            data=data)
        finally:
            jedi.set_debug_function(None)
