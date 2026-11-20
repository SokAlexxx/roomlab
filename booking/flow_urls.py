from django.urls import path

from booking import flow

urlpatterns = [
    path("bookings/mine/", flow.mine, name="bookings-mine"),
    path("bookings/<int:booking_id>/cancel/", flow.cancel, name="bookings-cancel"),
    path("approvals/", flow.approvals, name="approvals"),
    path("approvals/<int:booking_id>/decision/", flow.decide, name="approvals-decision"),
]
