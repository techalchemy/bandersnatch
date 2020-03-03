import contextlib
import filecmp
import hashlib
import io
import logging
import os
import pathlib
import shutil
import tempfile
from typing import (
    IO,
    Any,
    ContextManager,
    Dict,
    Generator,
    List,
    Optional,
    Type,
    Union,
    overload,
)

from bandersnatch.storage import StoragePlugin

logger = logging.getLogger("bandersnatch")


PATH_TYPES = Union[pathlib.PurePath, str]


class FilesystemStorage(StoragePlugin):
    name = "filesystem"
    PATH_BACKEND: Type[pathlib.PurePath] = pathlib.Path

    def walk(self, root: PATH_TYPES, dirs: bool = True) -> List[pathlib.Path]:
        if not isinstance(root, pathlib.Path):
            root = pathlib.Path(str(root))

        results: List[pathlib.Path] = []
        for pth in root.iterdir():
            if pth.is_dir():
                if dirs:
                    results.append(pth)
                for subpath in self.walk(pth, dirs=dirs):
                    results.append(pth / subpath)
            else:
                results.append(pth)
        return results

    def find(self, root: PATH_TYPES, dirs: bool = True) -> str:
        """A test helper simulating 'find'.

        Iterates over directories and filenames, given as relative paths to the
        root.

        """
        results = self.walk(root, dirs=dirs)
        results.sort()
        return "\n".join(str(result.relative_to(root)) for result in results)

    @contextlib.contextmanager
    def rewrite(
        self, filepath: PATH_TYPES, mode: str = "w", **kw: Any
    ) -> Generator[IO, None, None]:
        """Rewrite an existing file atomically to avoid programs running in
        parallel to have race conditions while reading."""
        # TODO: Account for alternative backends
        if isinstance(filepath, str):
            base_dir = os.path.dirname(filepath)
            filename = os.path.basename(filepath)
        else:
            base_dir = str(filepath.parent)
            filename = filepath.name

        # Change naming format to be more friendly with distributed POSIX
        # filesystems like GlusterFS that hash based on filename
        # GlusterFS ignore '.' at the start of filenames and this avoid rehashing
        with tempfile.NamedTemporaryFile(
            mode=mode, prefix=f".{filename}.", delete=False, dir=base_dir, **kw
        ) as f:
            filepath_tmp = f.name
            yield f

        if not self.exists(filepath_tmp):
            # Allow our clients to remove the file in case it doesn't want it to be
            # put in place actually but also doesn't want to error out.
            return
        os.chmod(filepath_tmp, 0o100644)
        self.copy_file(filepath_tmp, filepath)

    @contextlib.contextmanager
    def update_safe(self, filename: str, **kw: Any) -> Generator[IO, None, None]:
        """Rewrite a file atomically.

        Clients are allowed to delete the tmpfile to signal that they don't
        want to have it updated.

        """
        with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(filename),
            delete=False,
            prefix=f"{os.path.basename(filename)}.",
            **kw,
        ) as tf:
            if self.exists(filename):
                os.chmod(tf.name, os.stat(filename).st_mode & 0o7777)
            tf.has_changed = False  # type: ignore
            yield tf
            if not self.exists(tf.name):
                return
            filename_tmp = tf.name
        if self.exists(filename) and self.compare_files(filename, filename_tmp):
            os.unlink(filename_tmp)
        else:
            # TODO: account for other backends (add comparison functionality)
            self.copy_file(filename_tmp, filename)
            tf.has_changed = True  # type: ignore

    def compare_files(self, file1: PATH_TYPES, file2: PATH_TYPES) -> bool:
        """Compare two files, returning true if they are the same and False if they are not."""
        return filecmp.cmp(str(file1), str(file2), shallow=False)

    def copy_file(self, source: PATH_TYPES, dest: PATH_TYPES) -> None:
        """Copy a file from **source** to **dest**"""
        if not self.exists(source):
            raise FileNotFoundError(source)
        os.rename(source, dest)
        return

    def write_file(self, path: PATH_TYPES, contents: Union[str, bytes]) -> None:
        """Write data to the provided path.  If **contents** is a string, the file will
        be opened and written in "r" + "utf-8" mode, if bytes are supplied it will be
        accessed using "rb" mode (i.e. binary write)."""
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        if isinstance(contents, str):
            path.write_text(contents)
        else:
            path.write_bytes(contents)

    @overload
    @contextlib.contextmanager
    def open_file(self, path: PATH_TYPES, text: bool = True):
        ...

    @overload
    @contextlib.contextmanager
    def open_file(self, path: PATH_TYPES, text: bool = True, encoding: str = "utf-8"):
        ...

    @contextlib.contextmanager
    def open_file(
        self, path: PATH_TYPES, text: bool = True, encoding: str = "utf-8"
    ) -> Generator[IO, None, None]:
        """Yield a file context to iterate over. If text is true, open the file with
        'rb' mode specified."""
        mode = "r" if text else "rb"
        kwargs: Dict[str, str] = {}
        if text:
            kwargs["encoding"] = encoding
        with open(path, mode=mode, **kwargs) as fh:  # type: ignore
            yield fh

    def read_file(
        self,
        path: PATH_TYPES,
        text=True,
        encoding: str = "utf-8",
        errors: Optional[str] = None,
    ) -> Union[str, bytes]:
        """Return the contents of the requested file, either a a bytestring or a unicode
        string depending on whether **text** is True"""
        with self.open_file(path, text=text, encoding=encoding) as fh:
            contents = fh.read()
        return contents

    def delete_file(self, path: PATH_TYPES, dry_run: bool = False) -> int:
        """Delete the provided path, recursively if necessary."""
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        log_prefix = "[DRY RUN] " if dry_run else ""
        logger.info(f"{log_prefix}Removing file: {path!s}")
        if not dry_run:
            path.unlink()
        return 0

    def mkdir(
        self, path: PATH_TYPES, exist_ok: bool = False, parents: bool = False
    ) -> None:
        """Create the provided directory"""
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        return path.mkdir(exist_ok=exist_ok, parents=parents)

    def rmdir(
        self,
        path: PATH_TYPES,
        recurse: bool = False,
        force: bool = False,
        ignore_errors: bool = False,
        dry_run: bool = False,
    ) -> int:
        """Remove the directory. If recurse is True, allow removing empty children.
        If force is true, remove contents destructively."""
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        log_prefix = "[DRY RUN] " if dry_run else ""
        if force:
            logger.info(f"{log_prefix}Forcing removal of files under {path!s}")
            if not dry_run:
                shutil.rmtree(path, ignore_errors=ignore_errors)
                return 0
        if recurse:
            for subdir in path.iterdir():
                if not subdir.is_dir():
                    continue
                logger.info(f"{log_prefix}Removing directory: {subdir!s}")
                if not dry_run:
                    rc = self.rmdir(
                        subdir,
                        recurse=recurse,
                        force=force,
                        ignore_errors=ignore_errors,
                    )
                    if rc != 0:
                        return rc
        logger.info(f"{log_prefix}Removing directory: {path!s}")
        if not dry_run:
            path.rmdir()
        return 0

    def exists(self, path: PATH_TYPES) -> bool:
        """Check whether the provided path exists"""
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        return path.exists()

    def is_dir(self, path: PATH_TYPES) -> bool:
        """Check whether the provided path is a directory."""
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        return path.is_dir()

    def is_file(self, path: PATH_TYPES) -> bool:
        """Check whether the provided path is a file."""
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        return path.is_file()

    def get_hash(self, path: str, function: str = "sha256") -> str:
        h = getattr(hashlib, function)()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(128 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
