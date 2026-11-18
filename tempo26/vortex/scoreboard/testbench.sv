`timescale 1ns/1ps
`default_nettype none
module VX_scoreboard_tb;
    localparam NUM_WARPS = 4;
    localparam NUM_REGS = 32;
    localparam REGW = 5;
    localparam WIDW = 2;
    localparam TAGW = 8;
    localparam OPW = 8;
    localparam COUNTW = 16;

    logic clk = 0;
    logic reset = 1;
    logic flush = 0;
    logic [NUM_WARPS-1:0] req_valid = '0;
    wire [NUM_WARPS-1:0] req_ready;
    logic [NUM_WARPS-1:0][2:0] req_used_rs = '0;
    logic [NUM_WARPS-1:0][REGW-1:0] req_rs1 = '0;
    logic [NUM_WARPS-1:0][REGW-1:0] req_rs2 = '0;
    logic [NUM_WARPS-1:0][REGW-1:0] req_rs3 = '0;
    logic [NUM_WARPS-1:0] req_wb = '0;
    logic [NUM_WARPS-1:0][REGW-1:0] req_rd = '0;
    logic [NUM_WARPS-1:0][TAGW-1:0] req_tag = '0;
    logic [NUM_WARPS-1:0][OPW-1:0] req_op = '0;
    logic wb_valid = 0;
    logic [WIDW-1:0] wb_wid = 0;
    logic [REGW-1:0] wb_rd = 0;
    wire issue_valid;
    logic issue_ready = 0;
    wire [WIDW-1:0] issue_wid;
    wire [REGW-1:0] issue_rd;
    wire issue_wb;
    wire [TAGW-1:0] issue_tag;
    wire [OPW-1:0] issue_op;
    wire [NUM_WARPS-1:0][NUM_REGS-1:0] inuse_mask;
    wire [NUM_WARPS-1:0] staged_valid;
    wire [COUNTW-1:0] issue_count;
    wire [COUNTW-1:0] hazard_stall_count;
    wire [NUM_WARPS-1:0][COUNTW-1:0] warp_issue_count;

    integer errors = 0;
    integer checks = 0;
    always #5 clk = ~clk;

    VX_scoreboard #(
        .NUM_WARPS(NUM_WARPS),
        .NUM_REGS(NUM_REGS),
        .TAGW(TAGW),
        .OPW(OPW),
        .COUNTW(COUNTW)
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

    task automatic clear_requests;
        begin
            req_valid = '0;
            req_used_rs = '0;
            req_rs1 = '0;
            req_rs2 = '0;
            req_rs3 = '0;
            req_wb = '0;
            req_rd = '0;
            req_tag = '0;
            req_op = '0;
        end
    endtask

    initial begin
        repeat (2) @(posedge clk);
        @(negedge clk);
        reset = 0;
        check(req_ready == 4'b1111, "empty staging readiness");
        check(!issue_valid && staged_valid == 0 && inuse_mask == 0,
              "reset structural state");
        check(issue_count == 0 && hazard_stall_count == 0
              && warp_issue_count == 0, "reset counter state");

        // Warp zero instruction reserves r5 after leaving its stage.
        req_valid[0] = 1;
        req_wb[0] = 1;
        req_rd[0] = 5;
        req_tag[0] = 8'h10;
        req_op[0] = 8'hA0;
        @(posedge clk);
        #1;
        check(staged_valid[0], "request not captured in staging");
        @(negedge clk);
        clear_requests();
        @(posedge clk);
        #1;
        check(issue_valid, "ready instruction did not issue");
        check(issue_wid == 0, "first issue warp");
        check(issue_rd == 5 && issue_wb, "first issue destination");
        check(issue_tag == 8'h10 && issue_op == 8'hA0, "first issue payload");
        check(inuse_mask[0][5], "destination register not reserved");

        // Output payload must remain stable under backpressure.
        repeat (2) begin
            @(posedge clk);
            #1;
            check(issue_valid && issue_tag == 8'h10, "issue changed while stalled");
        end

        // Queue a dependent warp-zero request and independent warp-one request.
        @(negedge clk);
        issue_ready = 1;
        req_valid = 4'b0011;
        req_used_rs[0] = 3'b001;
        req_rs1[0] = 5;
        req_tag[0] = 8'h20;
        req_op[0] = 8'hB0;
        req_wb[1] = 1;
        req_rd[1] = 7;
        req_tag[1] = 8'h21;
        req_op[1] = 8'hB1;
        @(posedge clk);
        @(negedge clk);
        clear_requests();
        @(posedge clk);
        #1;
        check(issue_valid, "independent warp did not issue");
        check(issue_wid == 1 && issue_tag == 8'h21, "hazard arbitration result");
        check(staged_valid[0], "dependent instruction did not remain staged");
        check(inuse_mask[1][7], "warp-one destination not reserved");
        check(hazard_stall_count != 0, "hazard stall was not counted");

        // Complete warp-one output, leaving only the blocked warp-zero stage.
        @(posedge clk);
        #1;
        check(!issue_valid, "issue output did not drain");
        check(staged_valid[0], "blocked stage disappeared");

        // Same-cycle writeback bypass must make the dependent request issuable.
        @(negedge clk);
        wb_valid = 1;
        wb_wid = 0;
        wb_rd = 5;
        @(posedge clk);
        #1;
        check(issue_valid, "writeback release inserted an extra issue cycle");
        check(issue_wid == 0 && issue_tag == 8'h20, "released dependent payload");
        check(!inuse_mask[0][5], "writeback did not clear reservation");
        check(!staged_valid[0], "released instruction remained staged");

        @(negedge clk);
        wb_valid = 0;
        @(posedge clk);

        // Release warp one's outstanding r7 reservation.
        @(negedge clk);
        wb_valid = 1;
        wb_wid = 1;
        wb_rd = 7;
        @(posedge clk);
        #1;
        check(!inuse_mask[1][7], "warp-one writeback release");

        // Four simultaneous independent requests demonstrate RR fairness.
        @(negedge clk);
        wb_valid = 0;
        issue_ready = 1;
        req_valid = 4'b1111;
        req_tag[0] = 8'h30;
        req_tag[1] = 8'h31;
        req_tag[2] = 8'h32;
        req_tag[3] = 8'h33;
        req_op[0] = 8'hC0;
        req_op[1] = 8'hC1;
        req_op[2] = 8'hC2;
        req_op[3] = 8'hC3;
        @(posedge clk);
        @(negedge clk);
        clear_requests();
        repeat (4) begin
            @(posedge clk);
            #1;
            check(issue_valid, "fairness issue missing");
        end
        check(warp_issue_count[0] != 0, "warp zero never issued");
        check(warp_issue_count[1] != 0, "warp one never issued");
        check(warp_issue_count[2] != 0, "warp two never issued");
        check(warp_issue_count[3] != 0, "warp three never issued");

        // Flush removes staged and held output state as well as reservations.
        @(negedge clk);
        flush = 1;
        @(posedge clk);
        #1;
        check(staged_valid == 0, "flush did not clear stages");
        check(inuse_mask == 0, "flush did not clear reservations");
        check(!issue_valid, "flush did not clear output");
        check(issue_count >= 6, "issue count missed transfers");

        if (errors != 0) begin
            $fatal(1, "VX_scoreboard: %0d errors from %0d checks", errors, checks);
        end
        $display("PASS VX_scoreboard: %0d checks", checks);
        $finish;
    end
endmodule

`default_nettype wire
