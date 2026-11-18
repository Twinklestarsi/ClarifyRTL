`timescale 1ns/1ps
`default_nettype none
module VX_stream_arb_tb;
    localparam NUM_INPUTS = 4;
    localparam DATAW = 16;
    localparam COUNTW = 16;
    localparam SELW = 2;

    logic clk = 0;
    logic reset = 1;
    logic flush = 0;
    logic [NUM_INPUTS-1:0] valid_in = '0;
    wire [NUM_INPUTS-1:0] ready_in;
    logic [NUM_INPUTS-1:0][DATAW-1:0] data_in = '0;
    logic [NUM_INPUTS-1:0] last_in = '0;
    logic [NUM_INPUTS-1:0][1:0] priority_in = '0;
    wire valid_out;
    logic ready_out = 0;
    wire [DATAW-1:0] data_out;
    wire last_out;
    wire [SELW-1:0] sel_out;
    wire [1:0] queue_occupancy;
    wire grant_locked;
    wire [SELW-1:0] round_robin_ptr;
    wire [NUM_INPUTS-1:0][COUNTW-1:0] transfer_count;
    wire [NUM_INPUTS-1:0][COUNTW-1:0] stall_count;

    integer errors = 0;
    integer checks = 0;
    always #5 clk = ~clk;

    VX_stream_arb #(
        .NUM_INPUTS(NUM_INPUTS),
        .DATAW(DATAW),
        .COUNTW(COUNTW),
        .STICKY(1)
    ) dut (.*);

    task automatic check(input logic condition, input string message);
        begin
            checks = checks + 1;
            if (!condition) begin
                errors = errors + 1;
                $display("ERROR %0t: %s", $time, message);
            end
        end
    endtask

    task automatic drive_idle;
        begin
            valid_in = '0;
            last_in = '0;
            priority_in = '0;
        end
    endtask

    initial begin
        data_in[0] = 16'h1000;
        data_in[1] = 16'h2000;
        data_in[2] = 16'h3000;
        data_in[3] = 16'h4000;

        repeat (2) @(posedge clk);
        @(negedge clk);
        reset = 0;
        #1;
        check(!valid_out && queue_occupancy == 0 && !grant_locked
              && round_robin_ptr == 0, "reset queue/arbitration state");
        check(transfer_count == 0 && stall_count == 0,
              "stream counters did not reset");

        // A newly accepted beat must become visible on the next active edge.
        valid_in = 4'b0001;
        last_in = 4'b0001;
        priority_in[0] = 2'd1;
        #1;
        check(ready_in == 4'b0001, "single requester ready");
        @(posedge clk);
        #1;
        check(valid_out, "accepted beat did not become valid immediately");
        check(data_out == 16'h1000, "single requester payload");
        check(sel_out == 0, "single requester selector");
        check(last_out, "single requester last flag");
        check(queue_occupancy == 1, "queue occupancy after first push");

        // Backpressure must preserve the complete output tuple.
        @(negedge clk);
        drive_idle();
        ready_out = 0;
        repeat (2) begin
            @(posedge clk);
            #1;
            check(valid_out, "valid dropped under backpressure");
            check(data_out == 16'h1000, "data changed under backpressure");
            check(sel_out == 0, "selector changed under backpressure");
        end

        @(negedge clk);
        ready_out = 1;
        @(posedge clk);
        #1;
        check(!valid_out, "queue did not drain");

        // Highest priority wins even when round-robin points elsewhere.
        @(negedge clk);
        ready_out = 0;
        valid_in = 4'b0101;
        last_in = 4'b0101;
        priority_in = '0;
        priority_in[0] = 2'd1;
        priority_in[2] = 2'd3;
        #1;
        check(ready_in == 4'b0100, "priority winner was not input two");
        @(posedge clk);
        #1;
        check(valid_out && data_out == 16'h3000, "priority payload mismatch");
        check(sel_out == 2, "priority selector mismatch");

        @(negedge clk);
        drive_idle();
        ready_out = 1;
        @(posedge clk);

        // Non-last beat locks a packet even against a higher-priority request.
        @(negedge clk);
        ready_out = 0;
        valid_in = 4'b0010;
        last_in = 4'b0000;
        priority_in[1] = 2'd2;
        data_in[1] = 16'h2111;
        @(posedge clk);
        #1;
        check(grant_locked, "packet grant was not locked");
        check(valid_out && data_out == 16'h2111, "first packet beat mismatch");

        @(negedge clk);
        valid_in = 4'b1010;
        last_in = 4'b0010;
        priority_in[1] = 2'd0;
        priority_in[3] = 2'd3;
        data_in[1] = 16'h2222;
        #1;
        check(ready_in == 4'b0010, "sticky grant lost to priority");
        @(posedge clk);
        #1;
        check(queue_occupancy == 2, "second packet beat not buffered");
        check(!grant_locked, "packet lock did not release on last");

        // Drain both beats and verify their order.
        @(negedge clk);
        drive_idle();
        ready_out = 1;
        @(posedge clk);
        #1;
        check(valid_out && data_out == 16'h2222, "queued packet order mismatch");
        check(last_out, "second packet beat missing last");
        @(posedge clk);
        #1;
        check(!valid_out, "packet queue failed to empty");

        // Equal-priority continuous inputs must alternate from the RR pointer.
        @(negedge clk);
        ready_out = 1;
        valid_in = 4'b0011;
        last_in = 4'b0011;
        priority_in = '0;
        #1;
        check(ready_in == 4'b0001, "round-robin first equal contender");
        @(posedge clk);
        #1;
        check(valid_out && sel_out == 0, "first fair arbitration result");
        @(negedge clk);
        #1;
        check(ready_in == 4'b0010, "round-robin did not advance");
        @(posedge clk);
        #1;
        check(valid_out && sel_out == 1, "second fair arbitration result");

        // Flush clears queued traffic, packet lock, and ready during flush.
        @(negedge clk);
        flush = 1;
        #1;
        check(ready_in == 0, "ready asserted during flush");
        @(posedge clk);
        #1;
        check(!valid_out && queue_occupancy == 0, "flush did not clear queue");
        check(!grant_locked, "flush did not clear grant lock");

        @(negedge clk);
        flush = 0;
        drive_idle();
        check(transfer_count[0] >= 2, "input zero transfer counter");
        check(transfer_count[1] >= 2, "input one transfer counter");
        check(stall_count[3] >= 1, "contender stall counter");

        if (errors != 0) begin
            $fatal(1, "VX_stream_arb: %0d errors from %0d checks", errors, checks);
        end
        $display("PASS VX_stream_arb: %0d checks", checks);
        $finish;
    end
endmodule

`default_nettype wire
