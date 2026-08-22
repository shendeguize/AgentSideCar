import queue
import threading
import unittest

from sidecar.bus import EventBus, SubscriptionClosed
from sidecar.model import Event


class EventBusTests(unittest.TestCase):
    def test_slow_subscriber_drops_oldest_with_bounded_queue(self):
        bus = EventBus(queue_size=2)
        subscription = bus.subscribe()

        bus.publish({"value": 1})
        bus.publish({"value": 2})
        bus.publish({"value": 3})

        self.assertEqual(2, subscription.maxsize)
        self.assertEqual(1, subscription.dropped)
        self.assertEqual({"value": 2}, subscription.get(timeout=0.1))
        self.assertEqual({"value": 3}, subscription.get(timeout=0.1))
        subscription.close()

    def test_publish_copies_payload_for_each_subscriber(self):
        bus = EventBus()
        first = bus.subscribe()
        second = bus.subscribe()
        payload = {"value": 1}

        bus.publish(payload)
        payload["value"] = 99
        first_event = first.get(timeout=0.1)
        second_event = second.get(timeout=0.1)

        self.assertEqual({"value": 1}, first_event)
        self.assertEqual({"value": 1}, second_event)
        self.assertIsNot(first_event, second_event)
        first_event["value"] = 2
        self.assertEqual({"value": 1}, second_event)
        bus.close()

    def test_event_values_are_normalized_to_mappings(self):
        bus = EventBus()
        subscription = bus.subscribe()
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "fake",
            "one",
            "assistant",
            "hello",
        )

        bus.publish(event)

        self.assertEqual(event.to_dict(), subscription.get(timeout=0.1))
        with self.assertRaises(TypeError):
            bus.publish(object())
        bus.close()

    def test_unsubscribe_closes_once_and_stops_delivery(self):
        bus = EventBus(queue_size=1)
        subscription = bus.subscribe()
        bus.publish({"value": 1})

        subscription.close()

        self.assertTrue(subscription.closed)
        self.assertEqual(0, bus.subscriber_count)
        self.assertEqual(0, subscription.dropped)
        with self.assertRaisesRegex(SubscriptionClosed, "subscription is closed"):
            subscription.get(timeout=0.1)

        bus.unsubscribe(subscription)
        bus.publish({"value": 2})
        with self.assertRaises(queue.Empty):
            subscription.get(timeout=0.01)

    def test_close_finishes_current_and_future_subscriptions(self):
        bus = EventBus(queue_size=2)
        first = bus.subscribe()
        second = bus.subscribe()
        bus.publish({"value": 1})

        bus.close()
        bus.close()

        self.assertEqual(0, bus.subscriber_count)
        for subscription in (first, second):
            self.assertTrue(subscription.closed)
            self.assertEqual({"value": 1}, subscription.get(timeout=0.1))
            with self.assertRaises(SubscriptionClosed):
                subscription.get(timeout=0.1)

        late = bus.subscribe()
        self.assertTrue(late.closed)
        self.assertEqual(0, bus.subscriber_count)
        with self.assertRaises(SubscriptionClosed):
            late.get(timeout=0.1)

    def test_concurrent_publish_reaches_each_subscriber_without_loss(self):
        publisher_count = 4
        events_per_publisher = 50
        event_count = publisher_count * events_per_publisher
        bus = EventBus(queue_size=event_count)
        first = bus.subscribe()
        second = bus.subscribe()
        start = threading.Barrier(publisher_count)
        failures = []

        def publish(publisher):
            try:
                start.wait()
                for sequence in range(events_per_publisher):
                    bus.publish({"publisher": publisher, "sequence": sequence})
            except BaseException as error:
                failures.append(error)

        threads = [
            threading.Thread(target=publish, args=(publisher,))
            for publisher in range(publisher_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertFalse(failures)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        expected = {
            (publisher, sequence)
            for publisher in range(publisher_count)
            for sequence in range(events_per_publisher)
        }
        for subscription in (first, second):
            received = set()
            for _ in range(event_count):
                event = subscription.get(timeout=0.1)
                received.add((event["publisher"], event["sequence"]))
            self.assertEqual(expected, received)
            self.assertEqual(0, subscription.dropped)
        bus.close()


if __name__ == "__main__":
    unittest.main()
