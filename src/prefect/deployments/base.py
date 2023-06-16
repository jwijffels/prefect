"""
Core primitives for managing Prefect projects.  Projects provide a minimally opinionated
build system for managing flows and deployments.

To get started, follow along with [the project tutorial](/tutorials/projects/).
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import yaml

from prefect.flows import load_flow_from_entrypoint
from prefect.utilities.asyncutils import run_sync_in_worker_thread
from prefect.utilities.filesystem import create_default_ignore_file
from prefect.utilities.templating import apply_values


def find_prefect_directory(path: Path = None) -> Optional[Path]:
    """
    Given a path, recurses upward looking for .prefect/ directories.

    Once found, returns absolute path to the ./prefect directory, which is assumed to reside within the
    root for the current project.

    If one is never found, `None` is returned.
    """
    path = Path(path or ".").resolve()
    parent = path.parent.resolve()
    while path != parent:
        prefect_dir = path.joinpath(".prefect")
        if prefect_dir.is_dir():
            return prefect_dir

        path = parent.resolve()
        parent = path.parent.resolve()


def set_prefect_hidden_dir(path: str = None) -> bool:
    """
    Creates default `.prefect/` directory if one does not already exist.
    Returns boolean specifying whether or not a directory was created.

    If a path is provided, the directory will be created in that location.
    """
    path = Path(path or ".") / ".prefect"

    # use exists so that we dont accidentally overwrite a file
    if path.exists():
        return False
    path.mkdir(mode=0o0700)
    return True


def create_default_prefect_yaml(
    path: str, name: str = None, contents: dict = None
) -> bool:
    """
    Creates default `prefect.yaml` file in the provided path if one does not already exist;
    returns boolean specifying whether a file was created.

    Args:
        name (str, optional): the name of the project; if not provided, the current directory name
            will be used
        contents (dict, optional): a dictionary of contents to write to the file; if not provided,
            defaults will be used
    """
    path = Path(path)
    prefect_file = path / "prefect.yaml"
    if prefect_file.exists():
        return False
    default_file = Path(__file__).parent / "templates" / "prefect.yaml"

    if contents is None:
        with default_file.open(mode="r") as df:
            contents = yaml.safe_load(df)

    import prefect

    contents["prefect-version"] = prefect.__version__
    contents["name"] = name

    with prefect_file.open(mode="w") as f:
        # write header
        f.write(
            "# File for configuring project / deployment build, push and pull steps\n\n"
        )

        f.write("# Generic metadata about this project\n")
        yaml.dump({"name": contents["name"]}, f, sort_keys=False)
        yaml.dump({"prefect-version": contents["prefect-version"]}, f, sort_keys=False)
        f.write("\n")

        # build
        f.write("# build section allows you to manage and build docker images\n")
        yaml.dump({"build": contents["build"]}, f, sort_keys=False)
        f.write("\n")

        # push
        f.write(
            "# push section allows you to manage if and how this project is uploaded to"
            " remote locations\n"
        )
        yaml.dump({"push": contents["push"]}, f, sort_keys=False)
        f.write("\n")

        # pull
        f.write(
            "# pull section allows you to provide instructions for cloning this project"
            " in remote locations\n"
        )
        yaml.dump({"pull": contents["pull"]}, f, sort_keys=False)
        f.write("\n")

        # deployments
        f.write(
            "# the deployments section allows you to provide configuration for"
            " deploying flows\n"
        )
        yaml.dump({"deployments": contents.get("deployments", [])}, f, sort_keys=False)
    return True


def configure_project_by_recipe(recipe: str, **formatting_kwargs) -> dict:
    """
    Given a recipe name, returns a dictionary representing base configuration options.

    Args:
        recipe (str): the name of the recipe to use
        formatting_kwargs (dict, optional): additional keyword arguments to format the recipe

    Raises:
        ValueError: if provided recipe name does not exist.
    """
    # load the recipe
    recipe_path = Path(__file__).parent / "recipes" / recipe / "prefect.yaml"

    if not recipe_path.exists():
        raise ValueError(f"Unknown recipe {recipe!r} provided.")

    with recipe_path.open(mode="r") as f:
        config = yaml.safe_load(f)

    config = apply_values(
        template=config, values=formatting_kwargs, remove_notset=False
    )

    return config


def _get_git_remote_origin_url() -> Optional[str]:
    """
    Returns the git remote origin URL for the current directory.
    """
    try:
        origin_url = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            shell=sys.platform == "win32",
            stderr=subprocess.DEVNULL,
        )
        origin_url = origin_url.decode().strip()
    except subprocess.CalledProcessError:
        return None

    return origin_url


def _get_git_branch() -> Optional[str]:
    """
    Returns the git branch for the current directory.
    """
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        branch = branch.decode().strip()
    except subprocess.CalledProcessError:
        return None

    return branch


def initialize_project(
    name: str = None, recipe: str = None, inputs: dict = None
) -> List[str]:
    """
    Initializes a basic project structure with base files.  If no name is provided, the name
    of the current directory is used.  If no recipe is provided, one is inferred.

    Args:
        name (str, optional): the name of the project; if not provided, the current directory name
        recipe (str, optional): the name of the recipe to use; if not provided, one is inferred
        inputs (dict, optional): a dictionary of inputs to use when formatting the recipe

    Returns:
        List[str]: a list of files / directories that were created
    """
    # determine if in git repo or use directory name as a default
    is_git_based = False
    formatting_kwargs = {"directory": str(Path(".").absolute().resolve())}
    dir_name = os.path.basename(os.getcwd())

    remote_url = _get_git_remote_origin_url()
    if remote_url:
        formatting_kwargs["repository"] = remote_url
        is_git_based = True
        branch = _get_git_branch()
        formatting_kwargs["branch"] = branch or "main"

    formatting_kwargs["name"] = dir_name

    has_dockerfile = Path("Dockerfile").exists()

    if has_dockerfile:
        formatting_kwargs["dockerfile"] = "Dockerfile"
    elif recipe is not None and "docker" in recipe:
        formatting_kwargs["dockerfile"] = "auto"

    # hand craft a pull step
    if is_git_based and recipe is None:
        if has_dockerfile:
            recipe = "docker-git"
        else:
            recipe = "git"
    elif recipe is None and has_dockerfile:
        recipe = "docker"
    elif recipe is None:
        recipe = "local"

    formatting_kwargs.update(inputs or {})
    configuration = configure_project_by_recipe(recipe=recipe, **formatting_kwargs)

    project_name = name or dir_name

    files = []
    if create_default_ignore_file("."):
        files.append(".prefectignore")
    if create_default_prefect_yaml(".", name=project_name, contents=configuration):
        files.append("prefect.yaml")
    if set_prefect_hidden_dir():
        files.append(".prefect/")

    return files


async def register_flow(entrypoint: str, force: bool = False):
    """
    Register a flow with this project from an entrypoint.

    Args:
        entrypoint (str): the entrypoint to the flow to register
        force (bool, optional): whether or not to overwrite an existing flow with the same name

    Raises:
        ValueError: if `force` is `False` and registration would overwrite an existing flow
    """
    try:
        fpath, obj_name = entrypoint.rsplit(":", 1)
    except ValueError as exc:
        if str(exc) == "not enough values to unpack (expected 2, got 1)":
            missing_flow_name_msg = (
                "Your flow entrypoint must include the name of the function that is"
                f" the entrypoint to your flow.\nTry {entrypoint}:<flow_name>"
            )
            raise ValueError(missing_flow_name_msg)
        else:
            raise exc

    flow = await run_sync_in_worker_thread(load_flow_from_entrypoint, entrypoint)

    fpath = Path(fpath).absolute()
    prefect_dir = find_prefect_directory()
    if not prefect_dir:
        raise FileNotFoundError(
            "No .prefect directory could be found - run `prefect project"
            " init` to create one."
        )

    entrypoint = f"{fpath.relative_to(prefect_dir.parent)!s}:{obj_name}"

    flows_file = prefect_dir / "flows.json"
    if flows_file.exists():
        with flows_file.open(mode="r") as f:
            flows = json.load(f)
    else:
        flows = {}

    ## quality control
    if flow.name in flows and flows[flow.name] != entrypoint:
        if not force:
            raise ValueError(
                "Conflicting entry found for flow with name"
                f" {flow.name!r}:\n{flow.name}: {flows[flow.name]}"
            )
    flows[flow.name] = entrypoint

    with flows_file.open(mode="w") as f:
        json.dump(flows, f, sort_keys=True, indent=2)

    return flow


def _copy_deployments_into_prefect_file():
    """
    Copy deployments from the `deloyment.yaml` file into the `prefect.yaml` file.

    Used to migrate users from the old `prefect.yaml` + `deployment.yaml` structure
    to a single `prefect.yaml` file.
    """
    prefect_file = Path("prefect.yaml")
    deployment_file = Path("deployment.yaml")
    if not deployment_file.exists() or not prefect_file.exists():
        raise FileNotFoundError(
            "Could not find `prefect.yaml` or `deployment.yaml` files."
        )

    with deployment_file.open(mode="r") as f:
        raw_deployment_file_contents = f.read()
        parsed_deployment_file_contents = yaml.safe_load(raw_deployment_file_contents)

    deployments = parsed_deployment_file_contents.get("deployments")

    with prefect_file.open(mode="a") as f:
        # If deployment.yaml is empty, write an empty deployments list to prefect.yaml.
        if not parsed_deployment_file_contents:
            f.write("\n")
            f.write(yaml.dump({"deployments": []}, sort_keys=False))
        # If there is no 'deployments' key in deployment.yaml, assume that the
        # entire file is a single deployment.
        elif not deployments:
            f.write("\n")
            f.write(
                yaml.dump(
                    {"deployments": [parsed_deployment_file_contents]}, sort_keys=False
                )
            )
        # Write all of deployment.yaml to prefect.yaml.
        else:
            f.write("\n")
            f.write(raw_deployment_file_contents)