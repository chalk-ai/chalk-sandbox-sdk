from __future__ import annotations

import subprocess
import sys
import textwrap


def _run_python(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_generated_chalk_protos_do_not_register_in_default_pool() -> None:
    _run_python(
        """
        from google.protobuf import descriptor_pool

        from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2
        from chalkcompute._gen.chalk.container.v1 import service_pb2 as container_pb2
        from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb2 as catalog_pb2
        from chalkcompute._gen.chalk.runtime.v1 import remote_python_call_pb2
        from chalkcompute._gen.chalk.sandbox.v1 import service_pb2 as sandbox_pb2
        from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb2 as scaling_pb2
        from chalkcompute._gen.chalk.server.v1 import environment_secrets_pb2
        from chalkcompute._gen.chalk.volume.v1 import volume_pb2

        generated = [
            arrow_pb2.DESCRIPTOR,
            container_pb2.DESCRIPTOR,
            catalog_pb2.DESCRIPTOR,
            remote_python_call_pb2.DESCRIPTOR,
            sandbox_pb2.DESCRIPTOR,
            scaling_pb2.DESCRIPTOR,
            environment_secrets_pb2.DESCRIPTOR,
            volume_pb2.DESCRIPTOR,
        ]
        assert all(
            descriptor.pool is not descriptor_pool.Default()
            for descriptor in generated
        )

        for name in (
            "chalk/arrow/v1/arrow.proto",
            "chalk/auth/v1/permissions.proto",
            "chalk/common/v1/chalk_error.proto",
            "chalk/container/v1/service.proto",
            "chalk/externalfunctioncatalog/v1/service.proto",
            "chalk/runtime/v1/remote_python_call.proto",
            "chalk/sandbox/v1/service.proto",
            "chalk/scalinggroup/v1/service.proto",
            "chalk/server/v1/environment_secrets.proto",
            "chalk/utils/v1/encoding.proto",
            "chalk/volume/v1/volume.proto",
        ):
            try:
                descriptor_pool.Default().FindFileByName(name)
            except KeyError:
                pass
            else:
                raise AssertionError(f"{name} was registered in the default pool")
        """
    )


def test_generated_chalk_protos_import_with_conflicting_default_descriptor() -> None:
    _run_python(
        """
        from google.protobuf import descriptor_pb2, descriptor_pool

        fake_file = descriptor_pb2.FileDescriptorProto()
        fake_file.name = "chalk/sandbox/v1/service.proto"
        fake_file.package = "chalk.sandbox.v1"
        fake_message = fake_file.message_type.add()
        fake_message.name = "ExecRequest"
        fake_field = fake_message.field.add()
        fake_field.name = "different_field"
        fake_field.number = 1
        fake_field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        fake_field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING

        descriptor_pool.Default().AddSerializedFile(fake_file.SerializeToString())

        from chalkcompute._gen.chalk.sandbox.v1 import service_pb2

        request = service_pb2.ExecRequest()
        assert request.DESCRIPTOR.file.name == "chalk/sandbox/v1/service.proto"
        assert request.DESCRIPTOR.file.pool is not descriptor_pool.Default()
        """
    )
