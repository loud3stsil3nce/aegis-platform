import unittest

try:
    from src.approvals import arguments_hash, canonical_arguments
except ModuleNotFoundError:
    arguments_hash = canonical_arguments = None


@unittest.skipIf(arguments_hash is None, "SQLAlchemy is installed in the service image")
class ApprovalContractTests(unittest.TestCase):
    def test_argument_hash_is_order_independent(self):
        self.assertEqual(arguments_hash({"a": 1, "b": 2}), arguments_hash({"b": 2, "a": 1}))

    def test_argument_hash_changes_with_exact_action(self):
        self.assertNotEqual(arguments_hash({"target": "a"}), arguments_hash({"target": "b"}))

    def test_arguments_must_be_object(self):
        with self.assertRaises(TypeError):
            canonical_arguments(["not", "an", "object"])


if __name__ == "__main__":
    unittest.main()
